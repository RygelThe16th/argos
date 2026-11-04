"""Comparison-star ensemble table (WS7 revival).

:class:`ComparisonEnsembleTable` — the comp ensemble actually *in use* for the
night, read off the target set (role == ``comparison``), with their catalog
mags and a remove button. Lives as a tab of the Photometry window so the user
can see and prune the ensemble that forms the differential zero-point.
"""

from __future__ import annotations

from PyQt6.QtCore import QSignalBlocker, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from argos.core.imaging.platesolve import format_dec_dms, format_ra_hms
from argos.ui import theme

_ENSEMBLE_HEADERS = (
    "Name",
    "AUID",
    "RA (J2000)",
    "Dec (J2000)",
    "Catalogue mags",
    "Source",
    "From target",
    "To edge (px)",
)
_PLACEMENT_COLUMNS = (
    _ENSEMBLE_HEADERS.index("From target"),
    _ENSEMBLE_HEADERS.index("To edge (px)"),
)
_QUALITY_HEADERS = ("Samples", "Scatter (mag)", "Formal error (mag)", "Live status")


def _placement_values(placement, limits: tuple[float, float] | None) -> tuple:
    """Format separation from the target and distance to the frame edge.

    Returns ``(separation_text, edge_text, warning)`` where ``warning`` names
    the broken automatic-selection rule, if any, for the row tooltip.
    """
    if placement is None:
        return "—", "—", ""
    sep = placement.separation_arcsec
    sep_text = f"{sep:.0f}″" if sep < 120.0 else f"{sep / 60.0:.1f}′"
    edge = placement.edge_distance_px
    on_wcs = placement.target_distance_px is not None
    if edge is not None:
        edge_text = f"{edge:.0f}"
    else:
        edge_text = "outside" if on_wcs else "—"
    warnings = []
    if limits is not None and on_wcs:
        min_separation_px, edge_margin_px = limits
        if placement.target_distance_px < min_separation_px:
            warnings.append(f"inside the target's aperture + annulus ({min_separation_px:.0f} px)")
        if edge is None or edge < edge_margin_px:
            warnings.append(f"within the {edge_margin_px:.0f} px edge margin")
    return sep_text, edge_text, "; ".join(warnings)


def _quality_values(entry: dict | None) -> tuple[str, str, str, str]:
    """Format one persisted leave-one-out result for an observer-facing table."""
    if not entry:
        return ("—", "—", "—", "Waiting for data")
    status = str(entry.get("status") or "insufficient_data")
    labels = {
        "stable": "Stable",
        "unstable": "Unstable",
        "noisy": "Noisy",
        "insufficient_data": "Waiting for 10 points",
    }
    scatter = entry.get("scatter_mag")
    formal = entry.get("median_formal_error_mag")
    return (
        str(entry.get("n_valid") if entry.get("n_valid") is not None else "—"),
        "—" if scatter is None else f"{float(scatter):.4f}",
        "—" if formal is None else f"{float(formal):.4f}",
        labels.get(status, status.replace("_", " ").capitalize()),
    )


class ComparisonEnsembleTable(QWidget):
    """The comparison ensemble in use (target-set comps), with remove.

    Display only — the engine owns the ``TargetSet`` and persists/re-projects on
    the ``remove_requested`` signal (the ``TargetStar.key()`` of the dropped row).
    """

    remove_requested = pyqtSignal(str)
    star_selected = pyqtSignal(str)
    refresh_requested = pyqtSignal()
    recommend_requested = pyqtSignal()
    auto_count_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._hint = QLabel(
            "Comparison ensemble — catalogue magnitudes enable differential magnitudes; "
            "relative-flux preview only needs stable, unsaturated comparison stars."
        )
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color:{theme.FG_MUTED}; font-size:11px; padding:2px 0;")
        layout.addWidget(self._hint)

        proposal = QHBoxLayout()
        proposal.addWidget(QLabel("Automatically propose"))
        self._auto_count = QSpinBox()
        self._auto_count.setRange(1, 12)
        self._auto_count.setValue(5)
        self._auto_count.setSuffix(" calibrated stars")
        self._auto_count.setToolTip(
            "Number of calibrated VSP comparison stars Argos proposes when a target is selected"
        )
        self._auto_count.valueChanged.connect(self.auto_count_changed)
        proposal.addWidget(self._auto_count)
        proposal.addStretch(1)
        layout.addLayout(proposal)

        self._table = QTableWidget(0, len(_ENSEMBLE_HEADERS) + len(_QUALITY_HEADERS))
        self._table.setHorizontalHeaderLabels(list(_ENSEMBLE_HEADERS + _QUALITY_HEADERS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            len(_ENSEMBLE_HEADERS) + len(_QUALITY_HEADERS) - 1, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{theme.FG_MUTED};")
        row.addWidget(self._count)
        row.addStretch(1)
        self._remove_btn = QPushButton("Remove selected")
        self._remove_btn.clicked.connect(self._on_remove)
        row.addWidget(self._remove_btn)
        self._refresh_btn = QPushButton("Refresh catalogue")
        self._refresh_btn.setToolTip(
            "Fetch VSP comparison-star proposals again for the solved field"
        )
        self._refresh_btn.clicked.connect(self.refresh_requested)
        row.addWidget(self._refresh_btn)
        self._recommend_btn = QPushButton("Recommend from pilot")
        self._recommend_btn.setToolTip(
            "Replace VSP suggestions using SNR, saturation and brightness measured on the solved pilot image"
        )
        self._recommend_btn.clicked.connect(self.recommend_requested)
        row.addWidget(self._recommend_btn)
        copy_btn = QPushButton("Copy TSV")
        copy_btn.clicked.connect(self._on_copy)
        row.addWidget(copy_btn)
        layout.addLayout(row)
        self._keys: list[str] = []
        self._stars: list = []
        self._quality_by_key: dict[str, dict] = {}
        self._placement_by_key: dict[str, object] = {}
        self._placement_limits: tuple[float, float] | None = None

    def set_auto_count(self, count: int) -> None:
        """Synchronise the user preference without emitting a new request."""
        blocker = QSignalBlocker(self._auto_count)
        self._auto_count.setValue(max(1, int(count)))
        del blocker

    def set_targets(self, stars) -> None:
        """Populate from a full target set — filters to the comparison stars."""
        comps = [s for s in stars if s.role == "comparison"]
        self._stars = list(stars)
        self._keys = [s.key() for s in comps]
        blocker = QSignalBlocker(self._table)
        self._table.clearSelection()
        self._table.setRowCount(len(comps))
        for r, s in enumerate(comps):
            mags = "  ".join(f"{b} {m:.2f}" for b, m in s.mags.items()) or "—"
            values = (
                s.display_name,
                s.auid or "—",
                format_ra_hms(s.ra_deg / 15.0),
                format_dec_dms(s.dec_deg),
                mags,
                s.source or "manual",
            )
            values += ("—", "—")  # placement, filled by _show_placement
            quality = self._quality_by_key.get(s.auid or s.display_name)
            values += _quality_values(quality)
            for c, v in enumerate(values):
                self._table.setItem(r, c, QTableWidgetItem(v))
            self._show_placement(r, s.key())
        del blocker
        calibrated = [s for s in comps if s.mags]
        manual = len(comps) - len(calibrated)
        if not comps:
            self._count.setText(
                "No comparison stars — refresh the catalogue after a successful solve."
            )
        elif manual:
            self._count.setText(
                f"{len(calibrated)} calibrated comparison(s) · {manual} uncalibrated manual star(s)"
            )
        else:
            self._count.setText(f"{len(calibrated)} calibrated comparison star(s)")

    def set_placements(
        self, placements: dict | None, limits: tuple[float, float] | None = None
    ) -> None:
        """Show each star's separation from the target and distance to the edge.

        ``placements`` maps ``TargetStar.key()`` to a
        :class:`~argos.core.catalog.photometry.FieldPlacement` measured on the
        current WCS; ``limits`` is ``(min_separation_px, edge_margin_px)``.
        """
        self._placement_by_key = dict(placements or {})
        self._placement_limits = limits
        # Refreshed on every re-projection: update the two cells in place so
        # the observer's row selection survives.
        for row, key in enumerate(self._keys):
            self._show_placement(row, key)

    def _show_placement(self, row: int, key: str) -> None:
        sep_text, edge_text, warning = _placement_values(
            self._placement_by_key.get(key), self._placement_limits
        )
        for column, text in zip(_PLACEMENT_COLUMNS, (sep_text, edge_text)):
            item = QTableWidgetItem(text)
            if warning:
                item.setToolTip(f"Breaks the automatic-selection rule: {warning}")
                if theme.WARNING:
                    item.setForeground(QColor(theme.WARNING))
            self._table.setItem(row, column, item)

    def set_quality_report(self, report: dict | None) -> None:
        """Show the latest non-destructive leave-one-out assessment."""
        entries = (report or {}).get("comparison_stars") or []
        self._quality_by_key = {
            str(entry.get("auid") or entry.get("name")): entry
            for entry in entries
            if isinstance(entry, dict) and (entry.get("auid") or entry.get("name"))
        }
        self.set_targets(self._stars)

    def _on_remove(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._keys):
            self.remove_requested.emit(self._keys[row])

    def _on_selection_changed(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._keys):
            self.star_selected.emit(self._keys[row])

    def _on_copy(self) -> None:
        headers = _ENSEMBLE_HEADERS + _QUALITY_HEADERS
        lines = ["\t".join(headers)]
        for row in range(self._table.rowCount()):
            lines.append(
                "\t".join(
                    (self._table.item(row, column).text() if self._table.item(row, column) else "")
                    for column in range(len(headers))
                )
            )
        QApplication.clipboard().setText("\n".join(lines))


class ComparisonQualityTable(QWidget):
    """Read-only per-comparison quality report for Review.

    It intentionally reads the durable report created during acquisition rather
    than recomputing a result from a potentially incomplete CSV import.
    """

    star_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._hint = QLabel("No comparison-quality report in this session.")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color:{theme.FG_MUTED}; font-size:11px; padding:2px 0;")
        layout.addWidget(self._hint)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Name", *_QUALITY_HEADERS])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._table, 1)
        self._keys: list[str] = []

    def set_quality_report(self, report: dict | None) -> None:
        entries = (report or {}).get("comparison_stars") or []
        criteria = (report or {}).get("criteria") or {}
        if not entries:
            self._hint.setText("No comparison-quality report in this session.")
        else:
            min_points = criteria.get("min_points", 10)
            self._hint.setText(
                f"Leave-one-out live-preview diagnostic · minimum {min_points} valid points. "
                "Select a row to show its curve."
            )
        blocker = QSignalBlocker(self._table)
        self._table.clearSelection()
        self._table.setRowCount(len(entries))
        self._keys = []
        for row, entry in enumerate(entries):
            key = str(entry.get("auid") or entry.get("name") or "")
            self._keys.append(key)
            values = (str(entry.get("name") or key), *_quality_values(entry))
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(value))
        del blocker

    def _on_selection_changed(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._keys) and self._keys[row]:
            self.star_selected.emit(self._keys[row])
