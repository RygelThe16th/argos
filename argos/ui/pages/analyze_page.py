"""Analyze phase — vet the light curve and export AAVSO; inspect frames.

Two companion windows do the work (second-monitor friendly, so a finished night
can be vetted while a new run continues on Capture):

* **Light curve** — reload a session's measurements into the
  :class:`PhotometryWindow`, then export target-only AAVSO
  Extended Format stamped with the observer code + band from Settings.
* **Frame inspector** — open any FITS in the :class:`AnalysisWindow`.

The screen surfaces the observer code so it is obvious what an export will carry,
and warns when it is unset (AAVSO submissions need a real code).
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt, pyqtSlot
from PyQt6.QtGui import QAction, QShowEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QMenu,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from argos.core.catalog.targets import TargetSet
from argos.core.config import Config
from argos.core.photometry.lightcurve import read_curves_csv
from argos.core.session.review import SessionReviewError, load_session, load_session_curves
from argos.ui import design, theme
from argos.ui.widgets.comparison_curve_panel import ComparisonCurvePanel
from argos.ui.widgets.comparison_table import ComparisonQualityTable
from argos.ui.widgets.dock_host import make_dock, panel_toolbar_qss, style_toggle_action
from argos.ui.widgets.session_review import SessionQualityPlot
from argos.ui.widgets.target_curve_panel import TargetCurvePanel

logger = logging.getLogger(__name__)

_CFG_LAYOUT = "ui.review.layout"


def _format_metric(value) -> str:
    return "—" if value is None else f"{float(value):.2f}"


class AnalyzeScreen(QWidget):
    """Review a completed session before handing raw frames to post-processing."""

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        # Hold references so the spawned companion windows aren't garbage-collected.
        self._windows: list[QWidget] = []
        self._review = None
        self._review_curves: dict = {}
        self._review_frame_window = None

        self.setStyleSheet(f"background:{theme.BG};")
        root = QVBoxLayout(self)
        root.setContentsMargins(
            design.SPACING_XL, design.SPACING_LG, design.SPACING_XL, design.SPACING_LG
        )
        root.setSpacing(design.SPACING_MD)
        root.addWidget(design.HeadingLabel("Review"))
        self._session_card = self._build_session_card()
        root.addWidget(self._session_card)

        self._workspace = QMainWindow()
        self._workspace.setWindowFlags(Qt.WindowType.Widget)
        self._workspace.setDockNestingEnabled(True)
        self._workspace.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.GroupedDragging
        )
        # The session identity is a compact, fixed context strip above the
        # workspace.  The workspace centre is deliberately empty so panels
        # are the whole review surface and can be arranged without a fixed
        # non-scientific card competing with them.
        central = QWidget()
        central.setStyleSheet(f"background:{theme.BG};")
        self._workspace.setCentralWidget(central)
        self._build_review_docks()
        root.addWidget(self._build_panel_bar())
        # Docks retain useful plotting heights; when an observer builds a
        # taller review layout, the workspace scrolls instead of compressing
        # every scientific plot into unreadable strips.
        self._workspace.setMinimumHeight(660)
        self._review_scroll = QScrollArea()
        self._review_scroll.setWidgetResizable(True)
        self._review_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._review_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._review_scroll.setWidget(self._workspace)
        root.addWidget(self._review_scroll, 1)
        self._apply_default_layout()
        self._restore_layout()

        self._refresh_export_info()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_export_card(self) -> design.Card:
        card = design.Card("AAVSO export")
        form = QFormLayout()
        form.setContentsMargins(
            design.SPACING_MD, design.SPACING_LG, design.SPACING_MD, design.SPACING_MD
        )
        form.setHorizontalSpacing(design.SPACING_LG)
        form.setVerticalSpacing(design.SPACING_SM)
        self._obscode_value = design.MetricLabel("—")
        self._band_value = design.MetricLabel("—")
        form.addRow(design.MutedLabel("Observer code"), self._obscode_value)
        form.addRow(design.MutedLabel("Band / filter"), self._band_value)
        card.setLayout(form)
        return card

    def _build_session_card(self) -> design.Card:
        card = design.Card("")
        layout = design.card_layout(card)
        layout.setSpacing(design.SPACING_SM)
        heading = QHBoxLayout()
        self._session_title = design.MetricLabel("No session selected")
        heading.addWidget(self._session_title, 1)
        open_session = design.PrimaryButton("Open session…")
        open_session.setToolTip("Open the folder containing session.json")
        open_session.clicked.connect(self._open_session)
        heading.addWidget(open_session)
        layout.addLayout(heading)
        self._session_summary = design.MutedLabel("")
        self._session_summary.setWordWrap(True)
        self._session_summary.hide()
        layout.addWidget(self._session_summary)
        self._session_warnings = design.MutedLabel("")
        self._session_warnings.setWordWrap(True)
        self._session_warnings.setStyleSheet(f"color:{theme.WARNING};")
        layout.addWidget(self._session_warnings)

        card.setMaximumHeight(112)
        return card

    def _build_review_docks(self) -> None:
        """Create independently movable panels for the end-of-session review."""
        self._quality = SessionQualityPlot(metric="fwhm")
        self._curves = TargetCurvePanel()
        self._comparison_curves = ComparisonCurvePanel()
        self._comparison_quality = ComparisonQualityTable()
        self._curves.point_hovered.connect(self._on_curve_point_hovered)
        self._curves.point_clicked.connect(self._on_curve_point_clicked)
        self._comparison_curves.point_hovered.connect(self._on_curve_point_hovered)
        self._comparison_curves.point_clicked.connect(self._on_curve_point_clicked)
        self._comparison_quality.star_selected.connect(self._on_quality_star_selected)

        curves_page = QWidget()
        curves_layout = QVBoxLayout(curves_page)
        curves_layout.setContentsMargins(0, 0, 0, 0)
        curves_layout.addWidget(self._curves, 1)
        curve_row = QHBoxLayout()
        self._curve_point_info = design.MutedLabel(
            "Hover a point to inspect it; click to select it."
        )
        self._curve_point_info.setWordWrap(True)
        curve_row.addWidget(self._curve_point_info, 1)
        self._open_curve_frame_btn = design.SecondaryButton("Open source frame")
        self._open_curve_frame_btn.setEnabled(False)
        self._open_curve_frame_btn.clicked.connect(self._open_selected_curve_frame)
        curve_row.addWidget(self._open_curve_frame_btn)
        curves_layout.addLayout(curve_row)
        self._selected_curve_frame = None

        self._frames = QTableWidget(0, 9)
        self._frames.setHorizontalHeaderLabels(
            ["UTC", "Type", "Filter", "Exp (s)", "Gain", "FWHM", "HFD", "Temp (°C)", "File"]
        )
        self._frames.verticalHeader().setVisible(False)
        self._frames.horizontalHeader().setStretchLastSection(True)
        self._frames.cellDoubleClicked.connect(self._open_frame_from_table)
        self._metadata = QTableWidget(0, 2)
        self._metadata.setHorizontalHeaderLabels(["Field", "Value"])
        self._metadata.verticalHeader().setVisible(False)
        self._metadata.horizontalHeader().setStretchLastSection(True)
        export = self._build_export_card()
        self._docks: dict[str, QWidget] = {
            "source": make_dock(
                "Source light curve", curves_page, object_name="review.source_curve", scroll=False
            ),
            "fwhm": make_dock(
                "Frame quality — FWHM", self._quality, object_name="review.fwhm", scroll=False
            ),
            "comparison": make_dock(
                "Comparison stars",
                self._comparison_curves,
                object_name="review.comparison_curves",
                scroll=False,
            ),
            "comparison_quality": make_dock(
                "Comparison quality",
                self._comparison_quality,
                object_name="review.comparison_quality",
                scroll=False,
            ),
            "frames": make_dock("Frames", self._frames, object_name="review.frames", scroll=False),
            "metadata": make_dock(
                "Session metadata", self._metadata, object_name="review.metadata", scroll=False
            ),
            "export": make_dock("AAVSO export", export, object_name="review.export", scroll=False),
        }
        self._source_panels = [self._curves]
        self._source_docks = [self._docks["source"]]
        self._source_counter = 1
        self._comparison_panels = [self._comparison_curves]
        self._comparison_docks = [self._docks["comparison"]]
        self._comparison_counter = 1
        # These are the panels populated by the currently opened session.
        # Extra manually added panels remain possible, but a new session starts
        # with exactly one target/comparison plot per saved science star.
        self._active_target_count = 1
        self._active_comparison_count = 1
        # The shortest useful view of a time-series still needs room for axes,
        # legend and a readable scatter. These minima make the outer Review
        # scroll area take over when several plot panels are stacked.
        self._docks["source"].setMinimumHeight(280)
        self._docks["fwhm"].setMinimumHeight(240)
        self._docks["comparison"].setMinimumHeight(220)
        self._docks["comparison_quality"].setMinimumHeight(220)

    _PANEL_ORDER = (
        ("source", "Source curve"),
        ("fwhm", "FWHM"),
        ("comparison", "Comparison stars"),
        ("comparison_quality", "Comparison quality"),
        ("frames", "Frames"),
        ("metadata", "Metadata"),
        ("export", "AAVSO export"),
    )

    def _build_panel_bar(self) -> QToolBar:
        """Expose the dock model without asking an observer to discover Qt."""
        bar = QToolBar()
        bar.setMovable(False)
        bar.setStyleSheet(panel_toolbar_qss())
        open_session = bar.addAction("Open session…")
        open_session.triggered.connect(self._open_session)
        open_curve = bar.addAction("Open curve CSV…")
        open_curve.triggered.connect(self._open_lightcurve)
        inspect = bar.addAction("Open FITS…")
        inspect.triggered.connect(self._open_frame)
        add_comparison = bar.addAction("+ Comparison plot below")
        add_comparison.setToolTip(
            "Add an independently selectable comparison-star plot below the last comparison plot"
        )
        add_comparison.triggered.connect(self._add_comparison_plot)
        bar.addSeparator()
        self._panel_actions: dict[str, QAction] = {}
        for key, label in self._PANEL_ORDER:
            action = style_toggle_action(self._docks[key].toggleViewAction(), label)
            action.setToolTip(
                f"Show or hide {label}. Drag its title bar to arrange it; double-click to detach it."
            )
            self._panel_actions[key] = action
        for key in ("source", "fwhm", "comparison", "comparison_quality", "frames"):
            bar.addAction(self._panel_actions[key])
        more = QToolButton()
        more.setText("Panels ▾")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        for key in ("metadata", "export"):
            menu.addAction(self._panel_actions[key])
        more.setMenu(menu)
        bar.addWidget(more)
        bar.addSeparator()
        arrange = QToolButton()
        arrange.setText("Arrange ▾")
        arrange.setToolTip("Float, re-dock or reset the Review workspace")
        arrange.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._arrange_menu = QMenu(arrange)
        self._arrange_menu.aboutToShow.connect(self._refresh_arrange_menu)
        arrange.setMenu(self._arrange_menu)
        bar.addWidget(arrange)
        tile = bar.addAction("Tile plots — 2 columns")
        tile.setToolTip("Arrange active source, comparison and FWHM plots in a two-column grid")
        tile.triggered.connect(self._apply_default_layout)
        reset = bar.addAction("Reset layout")
        reset.triggered.connect(self.reset_layout)
        return bar

    # ------------------------------------------------------------------
    # Config-driven info
    # ------------------------------------------------------------------

    def _obscode(self) -> str:
        return str(self._config.get("observer.obscode", "") or "").strip()

    def _band(self) -> str:
        return str(self._config.get("photometry.default_band", "TG") or "TG").strip()

    def _refresh_export_info(self) -> None:
        code = self._obscode()
        if code:
            self._obscode_value.setText(code)
            self._obscode_value.setStyleSheet(
                f"color:{theme.ACCENT}; font-size:{design.FONT_SIZE_METRIC}px;"
                f" font-weight:bold; background:transparent;"
            )
        else:
            self._obscode_value.setText("unset — add it in Settings")
            self._obscode_value.setStyleSheet(
                f"color:{theme.WARNING}; font-size:{design.FONT_SIZE_METRIC}px;"
                f" font-weight:bold; background:transparent;"
            )
        self._band_value.setText(self._band())

    # ------------------------------------------------------------------
    # Dockable review workspace
    # ------------------------------------------------------------------

    def _apply_default_layout(self) -> None:
        """Tile the active science plots in two columns and as many rows as needed."""
        workspace = self._workspace
        right = Qt.DockWidgetArea.RightDockWidgetArea
        bottom = Qt.DockWidgetArea.BottomDockWidgetArea
        top = Qt.DockWidgetArea.TopDockWidgetArea
        # Snapshot visibility before removing docks. In particular, FWHM must
        # not reserve a phantom second cell after the observer hides it.
        source_plots = [
            dock for dock in self._source_docks[: self._active_target_count] if not dock.isHidden()
        ]
        comparison_plots = [
            dock
            for dock in self._comparison_docks[: self._active_comparison_count]
            if not dock.isHidden()
        ]
        show_fwhm = not self._docks["fwhm"].isHidden()
        for dock in (*self._docks.values(), *self._source_docks[1:], *self._comparison_docks[1:]):
            workspace.removeDockWidget(dock)

        # Review is a plot wall first. Build the grid exclusively from nested
        # splits: ``addDockWidget(Bottom)`` lets Qt create extra side bands on
        # some platforms, whereas this construction can never exceed two plot
        # columns.
        plots = [
            *source_plots,
            *comparison_plots,
            *([self._docks["fwhm"]] if show_fwhm else []),
        ]
        # A source panel is the permanent useful empty-session placeholder;
        # retain it only when every other plotting dock is intentionally hidden.
        show_placeholder = not plots
        if show_placeholder:
            plots = [self._docks["source"]]
        workspace.addDockWidget(top, plots[0])
        left_last = plots[0]
        right_last = None
        for index, dock in enumerate(plots[1:], start=1):
            if index == 1:
                # The second tile shares the current row with its left peer.
                workspace.splitDockWidget(left_last, dock, Qt.Orientation.Horizontal)
                right_last = dock
            elif index % 2 == 0:
                # Continue down the existing left/right split, never create a
                # third docking band.
                workspace.splitDockWidget(left_last, dock, Qt.Orientation.Vertical)
                left_last = dock
            else:
                workspace.splitDockWidget(right_last, dock, Qt.Orientation.Vertical)
                right_last = dock

        # Match Capture/Plan: establish a sensible starting height, then let
        # Qt's native splitters handle all later observer resizing and moving.
        for column in (plots[::2], plots[1::2]):
            if column:
                workspace.resizeDocks(column, [280] * len(column), Qt.Orientation.Vertical)

        # Tables and provenance information have stable homes but remain one
        # click away; they should not compete with the first scientific scan.
        workspace.addDockWidget(bottom, self._docks["frames"])
        workspace.addDockWidget(right, self._docks["metadata"])
        workspace.addDockWidget(right, self._docks["export"])
        workspace.addDockWidget(right, self._docks["comparison_quality"])
        workspace.tabifyDockWidget(self._docks["metadata"], self._docks["export"])
        workspace.tabifyDockWidget(self._docks["export"], self._docks["comparison_quality"])

        for key in ("frames", "metadata", "export"):
            self._docks[key].setVisible(False)
        self._docks["comparison_quality"].setVisible(True)
        self._docks["comparison_quality"].raise_()
        self._docks["fwhm"].setVisible(show_fwhm)
        for index, dock in enumerate(self._source_docks):
            dock.setVisible(dock in source_plots or (show_placeholder and index == 0))
        for index, dock in enumerate(self._comparison_docks):
            dock.setVisible(dock in comparison_plots)
        self._update_workspace_scroll_extent()

    def _restore_layout(self) -> None:
        blob = self._config.get(_CFG_LAYOUT)
        if not blob:
            return
        try:
            self._workspace.restoreState(QByteArray(base64.b64decode(blob)))
        except Exception as exc:  # noqa: BLE001 - corrupt preferences must not block review
            logger.warning("Review layout restore failed: %s", exc)

    def save_layout(self) -> None:
        """Persist the observer's Review panel arrangement on application close."""
        state = bytes(self._workspace.saveState())
        self._config.set(_CFG_LAYOUT, base64.b64encode(state).decode())

    def reset_layout(self) -> None:
        self._config.set(_CFG_LAYOUT, None)
        self._apply_default_layout()

    def _refresh_arrange_menu(self) -> None:
        self._arrange_menu.clear()
        tile = self._arrange_menu.addAction("Tile plots — 2 columns")
        tile.triggered.connect(self._apply_default_layout)
        reset = self._arrange_menu.addAction("Restore default layout")
        reset.triggered.connect(self.reset_layout)
        self._arrange_menu.addSeparator()
        for key, label in self._PANEL_ORDER:
            dock = self._docks[key]
            verb = "Dock" if dock.isFloating() else "Float"
            action = self._arrange_menu.addAction(f"{verb} {label}")
            action.triggered.connect(
                lambda _checked=False, panel=dock: self._toggle_dock_floating(panel)
            )
        for dock in self._source_docks[1:]:
            verb = "Dock" if dock.isFloating() else "Float"
            action = self._arrange_menu.addAction(f"{verb} {dock.windowTitle()}")
            action.triggered.connect(
                lambda _checked=False, panel=dock: self._toggle_dock_floating(panel)
            )

    def _add_comparison_plot(self) -> None:
        """Add a second (or later) independently selectable comparison curve."""
        self._comparison_counter += 1
        panel = ComparisonCurvePanel()
        panel.point_hovered.connect(self._on_curve_point_hovered)
        panel.point_clicked.connect(self._on_curve_point_clicked)
        selected = {item.selected_key() for item in self._comparison_panels}
        available = [
            str(key)
            for key, curve in self._review_curves.items()
            if getattr(curve, "role", "target") == "comparison" and str(key) not in selected
        ]
        panel.set_curves(self._review_curves, preferred_key=available[0] if available else "")
        dock = make_dock(
            f"Comparison star {self._comparison_counter}",
            panel,
            object_name=f"review.comparison_curve.{self._comparison_counter}",
            scroll=False,
        )
        dock.setMinimumHeight(220)
        # Use the current comparison dock as the split anchor. Adding first to
        # its own area (rather than always to Bottom) makes this an explicit
        # new row below the last comparison plot in the active user layout.
        anchor = self._comparison_docks[-1]
        area = self._workspace.dockWidgetArea(anchor)
        if area == Qt.DockWidgetArea.NoDockWidgetArea:
            area = Qt.DockWidgetArea.BottomDockWidgetArea
        self._workspace.addDockWidget(area, dock)
        self._workspace.splitDockWidget(anchor, dock, Qt.Orientation.Vertical)
        self._comparison_panels.append(panel)
        self._comparison_docks.append(dock)
        self._active_comparison_count = max(
            self._active_comparison_count, len(self._comparison_docks)
        )
        dock.show()
        dock.raise_()
        # The Review workspace lives in a scroll area.  Every independently
        # added comparison panel must therefore reserve another usable plot
        # height; otherwise QMainWindow keeps squeezing new vertical splits
        # into the existing two rows.
        self._update_workspace_scroll_extent()

    def _add_target_plot(self) -> tuple[TargetCurvePanel, QWidget]:
        """Create one movable Review dock for an additional science target."""
        self._source_counter += 1
        panel = TargetCurvePanel()
        panel.point_hovered.connect(self._on_curve_point_hovered)
        panel.point_clicked.connect(self._on_curve_point_clicked)
        dock = make_dock(
            f"Target {self._source_counter}",
            panel,
            object_name=f"review.source_curve.{self._source_counter}",
            scroll=False,
        )
        dock.setMinimumHeight(280)
        self._source_panels.append(panel)
        self._source_docks.append(dock)
        return panel, dock

    def _sync_target_plots(self) -> None:
        """Give every science target a source-curve dock by default."""
        targets = [
            (str(key), curve)
            for key, curve in self._review_curves.items()
            if getattr(curve, "role", "target") == "target"
        ]
        self._active_target_count = max(1, len(targets))
        while len(self._source_panels) < max(1, len(targets)):
            self._add_target_plot()
        for index, panel in enumerate(self._source_panels):
            dock = self._source_docks[index]
            if index < len(targets):
                key, curve = targets[index]
                panel.set_curves(self._review_curves, preferred_key=key)
                dock.setWindowTitle(f"Target — {curve.name or curve.auid or index + 1}")
                dock.setVisible(True)
            else:
                panel.set_curves({})
                dock.setWindowTitle("Target light curve")
                dock.setVisible(index == 0)
        self._update_workspace_scroll_extent(len(targets))

    def _sync_comparison_plots(self) -> None:
        """Show one independently selected comparison panel per saved star.

        The default Review scan must be immediately useful after opening a
        night: unlike the optional ``All comparison stars`` view, each saved
        comparison receives its own plot and scale.  Further panels can still
        be added manually for duplicate/all-star views.
        """
        comparisons = [
            (str(key), curve)
            for key, curve in self._review_curves.items()
            if getattr(curve, "role", "target") == "comparison"
        ]
        self._active_comparison_count = len(comparisons)
        while len(self._comparison_panels) < len(comparisons):
            self._add_comparison_plot()
        for index, panel in enumerate(self._comparison_panels):
            dock = self._comparison_docks[index]
            if index < len(comparisons):
                key, curve = comparisons[index]
                panel.set_curves(self._review_curves, preferred_key=key)
                dock.setWindowTitle(f"Comparison — {curve.name or curve.auid or index + 1}")
                dock.setVisible(True)
            else:
                panel.set_curves({})
                dock.setWindowTitle("Comparison star")
                dock.setVisible(False)
        self._update_workspace_scroll_extent()

    @pyqtSlot(str)
    def _on_quality_star_selected(self, key: str) -> None:
        """Make the Review quality table a direct route to its comparison curve."""
        if self._comparison_panels:
            self._comparison_panels[0].set_selected_key(key)
            self._docks["comparison"].setVisible(True)
            self._docks["comparison"].raise_()

    def _update_workspace_scroll_extent(self, target_count: int | None = None) -> None:
        """Let the outer Review scroll area grow instead of flattening plots.

        Source and comparison panels are independently addable movable docks.
        The default grid reserves a useful height for each *row* of two plots;
        the QScrollArea then supplies vertical scrolling instead of squeezing
        every scientific curve into a narrow strip.
        """
        if target_count is None:
            # Qt reports child docks as hidden while the enclosing Review page
            # is inactive. The active counts are therefore the only reliable
            # pre-show layout model; real geometry below refines it once shown.
            target_count = self._active_target_count
        comparison_count = self._active_comparison_count
        fwhm_count = 1
        plot_count = int(target_count) + int(comparison_count) + fwhm_count
        fallback_rows = max(2, (plot_count + 1) // 2)

        self._workspace.setMinimumHeight(max(660, 280 * fallback_rows))

    @staticmethod
    def _toggle_dock_floating(dock) -> None:
        dock.setFloating(not dock.isFloating())
        dock.show()
        dock.raise_()

    def showEvent(self, event: QShowEvent) -> None:
        self._refresh_export_info()  # observer code may have changed in Settings
        super().showEvent(event)

    # ------------------------------------------------------------------
    # Companion windows
    # ------------------------------------------------------------------

    def open_session_photometry(self) -> None:
        """Open saved measurements from the application File menu."""
        self._open_lightcurve()

    def _open_session(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Open Argos session folder", str(self._config.sessions_path)
        )
        if not folder:
            return
        try:
            review = load_session(folder)
        except SessionReviewError as exc:
            QMessageBox.warning(self, "Could not open session", str(exc))
            return
        self._review = review
        self._session_title.setText(f"{review.object_name} · {review.root.name}")
        types = ", ".join(f"{count} {name}" for name, count in review.image_type_counts.items())
        filters = ", ".join(f"{name}: {count}" for name, count in review.filter_counts.items())
        self._session_summary.setText(
            f"Started {review.started_utc or 'unknown'} · {len(review.frames)} frames ({types}) · "
            f"Filters: {filters or '—'} · {review.software}"
        )
        self._session_summary.show()
        self._quality.set_session(review)
        curves = load_session_curves(review)
        self._review_curves = curves
        self._comparison_quality.set_quality_report(review.comparison_quality)
        self._sync_target_plots()
        self._sync_comparison_plots()
        # A newly opened session defines the initial scientific layout: every
        # target is visible rather than hidden behind a single combined plot.
        self._apply_default_layout()
        self._selected_curve_frame = None
        self._open_curve_frame_btn.setEnabled(False)
        self._curve_point_info.setText(
            "Hover a point to inspect it; click to select its source frame."
        )
        self._populate_frames(review)
        self._populate_metadata(review)
        issues = review.readiness_issues()
        self._session_warnings.setText(
            " · ".join(issues) if issues else "Session structure looks complete."
        )
        self._session_warnings.show()
        for dock in (*self._docks.values(), *self._comparison_docks[1:]):
            dock.setEnabled(True)

    def _populate_frames(self, review) -> None:
        self._frames.setRowCount(len(review.frames))
        for row, frame in enumerate(review.frames):
            values = (
                frame.timestamp.isoformat(timespec="seconds") if frame.timestamp else "—",
                frame.image_type,
                frame.filter_name or "—",
                f"{frame.exposure_s:g}",
                str(frame.gain),
                _format_metric(frame.fwhm),
                _format_metric(frame.hfd),
                _format_metric(frame.ccd_temp),
                frame.filename,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, frame)
                self._frames.setItem(row, column, item)

    def _populate_metadata(self, review) -> None:
        values = [
            ("Session folder", str(review.root)),
            ("Object", review.object_name),
            ("Started UTC", review.started_utc or "—"),
            ("Software", review.software),
            ("Observer", review.observer or "—"),
            ("Frame count", str(len(review.frames))),
        ]
        values.extend((str(key), str(value)) for key, value in sorted(review.metadata.items()))
        self._metadata.setRowCount(len(values))
        for row, (key, value) in enumerate(values):
            self._metadata.setItem(row, 0, QTableWidgetItem(key))
            self._metadata.setItem(row, 1, QTableWidgetItem(value))

    def _on_curve_point_hovered(self, name: str, jd: float, mag: float, error: float) -> None:
        self._curve_point_info.setText(
            f"{name} · JD {jd:.6f} · preview value {mag:.4f} ± {error:.4f}"
        )

    def _on_curve_point_clicked(self, name: str, jd: float, mag: float, error: float) -> None:
        if self._review is None:
            return
        frame = self._review.nearest_light_frame(jd)
        self._selected_curve_frame = frame
        if frame is None:
            self._curve_point_info.setText(f"{name} · no logged light frame matches JD {jd:.6f}")
            self._open_curve_frame_btn.setEnabled(False)
            return
        self._curve_point_info.setText(
            f"{name} · {frame.filename} · {frame.timestamp.isoformat(timespec='seconds') if frame.timestamp else 'unknown UTC'} "
            f"· FWHM {_format_metric(frame.fwhm)} · HFD {_format_metric(frame.hfd)}"
        )
        self._open_curve_frame_btn.setEnabled(self._review.frame_path(frame) is not None)

    def _open_selected_curve_frame(self) -> None:
        if self._selected_curve_frame is not None:
            self._open_review_frame(self._selected_curve_frame)

    def _open_frame_from_table(self, row: int, _column: int) -> None:
        item = self._frames.item(row, 0)
        if item is not None:
            self._open_review_frame(item.data(Qt.ItemDataRole.UserRole))

    def _open_review_frame(self, frame) -> None:
        if self._review is None:
            return
        path = self._review.frame_path(frame)
        if path is None:
            QMessageBox.warning(self, "Frame unavailable", f"Could not find {frame.filename}.")
            return
        from argos.ui.analysis_window import AnalysisWindow

        window = self._review_frame_window
        if window is None:
            window = AnalysisWindow(self._config)
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            self._review_frame_window = window
            self._windows.append(window)
        if window.load(str(path)):
            window.show()
            window.raise_()
            window.activateWindow()

    def _open_lightcurve(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open session light curve",
            str(self._config.sessions_path),
            "CSV (*.csv);;All files (*)",
        )
        if not path:
            return
        curves = read_curves_csv(path)
        if not curves:
            logger.warning("No valid photometry rows in %s", path)
            return
        from argos.ui.panels.photometry_window import PhotometryWindow

        window = PhotometryWindow()
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        window.load_curves(
            curves,
            obscode=self._obscode() or "XXX",
            filt=self._band(),
        )
        # A session CSV sits in its run folder or the photometry/ subfolder,
        # beside the run's targets.json (whose comparisons name the AAVSO CHART).
        csv_dir = Path(path).parent
        for folder in (csv_dir, csv_dir.parent):
            if (folder / "targets.json").exists():
                window.set_targets(TargetSet.load(folder / "targets.json").stars)
                break
        window.show()
        window.raise_()
        self._windows.append(window)

    def _open_frame(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open FITS frame",
            str(self._config.sessions_path),
            "FITS (*.fits *.fit *.fts);;All files (*)",
        )
        if not path:
            return
        from argos.ui.analysis_window import AnalysisWindow

        window = AnalysisWindow(self._config)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        window.load(path)
        window.show()
        self._windows.append(window)
