"""Astrometry + catalog settings — a popup to tune solving & catalog queries.

A convenience dialog launched from the Shell's **Astrometry** menu (and from the
analysis window) so the user can adjust the plate-solve and AAVSO-catalog
parameters without hunting through the Settings page. It reads and writes the
**same config keys** as the main Configuration page, so the two stay in sync. On
save it emits :attr:`saved` — the Shell asks the engine to re-query the catalog
so changes (e.g. a brighter magnitude limit) apply on the live field immediately.

Why two tabs, not two dialogs
-----------------------------
The owner's mental model is two menu entries — "Plate solving…" and "Catalog…".
Rather than duplicate a form (and its load/save plumbing) across two dialogs, we
keep one dialog with two tabs and let the caller pick which tab opens first via
``section``. One save button writes every key at once, so the two related
parameter sets can be tuned together without closing and reopening.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# Single source of truth for the DB list + downsample options (shared with the
# main Configuration page).
from argos.ui.pages.configuration_page import _ASTAP_DATABASES, _DOWNSAMPLE

#: Selectable initial tab (also the config-namespace each tab writes into).
SECTION_ASTROMETRY = "astrometry"
SECTION_CATALOG = "catalog"
SECTION_RECENTER = "recenter"

#: Re-centering modes: (label, config value).
_RECENTER_MODES = (
    ("Off", "off"),
    ("When the target drifts past a threshold", "threshold"),
    ("Every N frames", "every_n"),
)


class AstrometrySettingsDialog(QDialog):
    """Edit astrometry + catalog settings; persist to the shared config.

    Args:
        config: the shared :class:`~argos.core.config.Config` (or a duck-typed
            stand-in with ``get``/``set``/``save`` — the tests use one).
        parent: Qt parent widget.
        section: which tab to open on — ``SECTION_ASTROMETRY`` (default) or
            ``SECTION_CATALOG``. Lets the two menu entries land on their tab.

    Signals:
        saved(): emitted after the settings are written, so callers can re-apply
            (the Shell triggers a catalog refetch on the running engine).
    """

    saved = pyqtSignal()

    def __init__(self, config, parent=None, *, section: str = SECTION_ASTROMETRY) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Field identification & catalogue settings")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_astrometry_tab(), "Plate solving")
        self._tabs.addTab(self._build_catalog_tab(), "Field catalogue")
        self._tabs.addTab(self._build_recenter_tab(), "Re-centering")
        layout.addWidget(self._tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load()
        # Open on the tab the caller asked for.
        self._tabs.setCurrentIndex({SECTION_CATALOG: 1, SECTION_RECENTER: 2}.get(section, 0))

    # ------------------------------------------------------------------

    def _build_astrometry_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        self._astap_edit = QLineEdit()
        self._astap_edit.setPlaceholderText("auto-detect (astap_cli / astap on PATH)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_astap)
        path_row = QHBoxLayout()
        path_row.addWidget(self._astap_edit, 1)
        path_row.addWidget(browse)
        form.addRow("ASTAP executable", path_row)

        self._db_combo = QComboBox()
        self._db_combo.addItems(_ASTAP_DATABASES)
        form.addRow("Star database", self._db_combo)

        self._radius_spin = QSpinBox()
        self._radius_spin.setRange(0, 180)
        self._radius_spin.setSuffix("°")
        self._radius_spin.setToolTip("Search radius around the pointing hint (0 = whole-sky blind)")
        form.addRow("Pointing search radius", self._radius_spin)

        self._down_combo = QComboBox()
        for label, _v in _DOWNSAMPLE:
            self._down_combo.addItem(label)
        form.addRow("Solve-image downsampling", self._down_combo)

        self._grid_spin = QSpinBox()
        self._grid_spin.setRange(0, 120)
        self._grid_spin.setSuffix("′")
        self._grid_spin.setToolTip("RA/Dec grid spacing in arcminutes (0 = auto, adaptive)")
        form.addRow("Coordinate-grid spacing", self._grid_spin)

        self._scale_hint_chk = QCheckBox("Use camera plate scale as a field-of-view hint")
        form.addRow("", self._scale_hint_chk)
        return tab

    def _build_catalog_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        help_label = QLabel(
            "These controls define what is fetched and displayed for a solved image. "
            "They do not change a FITS frame or the scientific photometry."
        )
        help_label.setWordWrap(True)
        form.addRow(help_label)

        self._mag_spin = QDoubleSpinBox()
        self._mag_spin.setRange(5.0, 20.0)
        self._mag_spin.setSingleStep(0.5)
        self._mag_spin.setDecimals(1)
        self._mag_spin.setToolTip("Drop catalog objects fainter than this (a dense field is huge)")
        form.addRow("VSX faint-end magnitude limit", self._mag_spin)

        self._max_spin = QSpinBox()
        self._max_spin.setRange(10, 2000)
        self._max_spin.setSingleStep(10)
        self._max_spin.setToolTip("Cap on variable stars drawn (brightest kept)")
        form.addRow("Maximum VSX variables in field", self._max_spin)

        self._suspected_chk = QCheckBox("Include suspected variables")
        form.addRow("", self._suspected_chk)

        self._identification_budget = QComboBox()
        for label, value in (
            ("Fast · 100 objects", 100),
            ("Fast · 200 objects", 200),
            ("Balanced · 400 objects", 400),
            ("Detailed · 800 objects", 800),
            ("Dense field · 1,600 objects", 1600),
        ):
            self._identification_budget.addItem(label, value)
        self._identification_budget.setToolTip(
            "Maximum online field identities shared between Gaia and SIMBAD. "
            "A smaller budget is faster. Scientific VSX/VSP results are not truncated by it."
        )
        self._identification_budget.currentIndexChanged.connect(self._apply_identification_budget)
        form.addRow("Field identification budget", self._identification_budget)

        self._field_catalogue_chk = QCheckBox("Identify ordinary field stars with Gaia DR3")
        self._field_catalogue_chk.setToolTip(
            "Queries Gaia after a field solve and uses the local cache when available. "
            "Gaia identifiers are for field recognition, not calibrated photometry."
        )
        form.addRow("", self._field_catalogue_chk)

        self._field_mag_spin = QDoubleSpinBox()
        self._field_mag_spin.setRange(3.0, 20.0)
        self._field_mag_spin.setSingleStep(0.5)
        self._field_mag_spin.setDecimals(1)
        self._field_mag_spin.setToolTip(
            "Only Gaia sources brighter than this G magnitude are shown"
        )
        form.addRow("Gaia cached-depth G limit", self._field_mag_spin)

        self._field_max_spin = QSpinBox()
        self._field_max_spin.setRange(10, 5000)
        self._field_max_spin.setSingleStep(10)
        self._field_max_spin.setToolTip("Cap on named Gaia source markers in the solved field")
        self._field_max_spin.hide()  # legacy key; controlled by the shared budget above

        self._field_depth = QComboBox()
        self._field_depth.addItem("Bright field · Gaia G ≤ 15", 15.0)
        self._field_depth.addItem("Standard field · Gaia G ≤ 18", 18.0)
        self._field_depth.addItem("Deep field · Gaia G ≤ 20", 20.0)
        self._field_depth.addItem("Custom", None)
        self._field_depth.currentIndexChanged.connect(self._apply_field_depth)
        form.addRow("Gaia cache depth", self._field_depth)

        self._field_detected_only_chk = QCheckBox(
            "Only draw Gaia sources with a local image detection"
        )
        self._field_detected_only_chk.setToolTip(
            "Off by default: after a good plate solve, Gaia/WCS coordinates are the "
            "identification reference. Enable only to declutter a noisy preview."
        )
        form.addRow("", self._field_detected_only_chk)

        self._essential_objects_chk = QCheckBox(
            "Show bundled Messier, NGC and IC objects in the solved field"
        )
        self._essential_objects_chk.setToolTip(
            "Works offline. This compact catalogue identifies deep-sky objects, not every star."
        )
        form.addRow("", self._essential_objects_chk)

        self._cached_exoplanets_chk = QCheckBox(
            "Show previously prepared exoplanet hosts from the local cache"
        )
        self._cached_exoplanets_chk.setToolTip(
            "Does not query the network and is not a complete exoplanet-field catalogue."
        )
        form.addRow("", self._cached_exoplanets_chk)

        self._named_objects_chk = QCheckBox(
            "Identify named objects and types with SIMBAD for every solved field"
        )
        self._named_objects_chk.setToolTip(
            "Uses the local field cache first, then SIMBAD after Field → Identify field. "
            "Gaia remains the complete star layer."
        )
        form.addRow("", self._named_objects_chk)

        self._named_max_spin = QSpinBox()
        self._named_max_spin.setRange(50, 2000)
        self._named_max_spin.setSingleStep(50)
        self._named_max_spin.setToolTip("Cap for named SIMBAD objects returned for a crowded field")
        self._named_max_spin.hide()  # legacy key; controlled by the shared budget above

        self._named_network_chk = QCheckBox("Allow SIMBAD lookup when this field is not cached")
        self._named_network_chk.setToolTip("Disable for a cache-only / offline observing session.")
        form.addRow("", self._named_network_chk)

        self._exoplanet_hosts_chk = QCheckBox(
            "Find confirmed exoplanet hosts for every solved field"
        )
        self._exoplanet_hosts_chk.setToolTip(
            "Uses the local field cache first, then NASA Exoplanet Archive when permitted."
        )
        form.addRow("", self._exoplanet_hosts_chk)

        self._exoplanet_network_chk = QCheckBox("Allow NASA lookup when this field is not cached")
        self._exoplanet_network_chk.setToolTip(
            "Disable for a cache-only / offline observing session."
        )
        form.addRow("", self._exoplanet_network_chk)

        # Photometry knob that belongs with the catalog: when a target is picked
        # the engine auto-fills the comparison ensemble from the field's VSP
        # stars — this caps how many it grabs.
        self._autocomp_spin = QSpinBox()
        self._autocomp_spin.setRange(1, 20)
        self._autocomp_spin.setToolTip(
            "How many comparison stars are auto-picked when a target is chosen"
        )
        form.addRow("Automatic reference stars", self._autocomp_spin)
        return tab

    def _build_recenter_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        help_label = QLabel(
            "During a sequence with a photometry target, nudge the mount back between "
            "exposures when the target drifts (unguided EQ tracking). Never moves during "
            "an exposure. The first correction calibrates each mount axis with a short "
            "pulse. Needs aperture tracking; off by default."
        )
        help_label.setWordWrap(True)
        form.addRow(help_label)

        self._recenter_mode = QComboBox()
        for label, value in _RECENTER_MODES:
            self._recenter_mode.addItem(label, value)
        self._recenter_mode.currentIndexChanged.connect(self._sync_recenter_enabled)
        form.addRow("Re-center", self._recenter_mode)

        self._recenter_threshold = QDoubleSpinBox()
        self._recenter_threshold.setRange(2.0, 500.0)
        self._recenter_threshold.setDecimals(0)
        self._recenter_threshold.setSuffix(" px")
        self._recenter_threshold.setToolTip(
            "Correct once the target is this far (green-plane pixels) from where it "
            "was on the first measured frame"
        )
        form.addRow("Drift threshold", self._recenter_threshold)

        self._recenter_every = QSpinBox()
        self._recenter_every.setRange(1, 999)
        self._recenter_every.setSuffix(" frames")
        form.addRow("Correct every", self._recenter_every)

        self._recenter_deadband = QDoubleSpinBox()
        self._recenter_deadband.setRange(0.0, 50.0)
        self._recenter_deadband.setDecimals(1)
        self._recenter_deadband.setSuffix(" px")
        self._recenter_deadband.setToolTip("Every-N mode: skip a correction smaller than this")
        form.addRow("Ignore drift below", self._recenter_deadband)

        self._recenter_max_step = QDoubleSpinBox()
        self._recenter_max_step.setRange(1.0, 500.0)
        self._recenter_max_step.setDecimals(0)
        self._recenter_max_step.setSuffix(" px")
        self._recenter_max_step.setToolTip("Largest correction applied in one move")
        form.addRow("Largest single correction", self._recenter_max_step)

        self._recenter_settle = QDoubleSpinBox()
        self._recenter_settle.setRange(0.0, 60.0)
        self._recenter_settle.setDecimals(1)
        self._recenter_settle.setSuffix(" s")
        self._recenter_settle.setToolTip("Wait after a move before the next exposure starts")
        form.addRow("Settle time", self._recenter_settle)
        return tab

    def _sync_recenter_enabled(self, _index: int = 0) -> None:
        mode = self._recenter_mode.currentData()
        self._recenter_threshold.setEnabled(mode == "threshold")
        self._recenter_every.setEnabled(mode == "every_n")
        self._recenter_deadband.setEnabled(mode == "every_n")
        for w in (self._recenter_max_step, self._recenter_settle):
            w.setEnabled(mode != "off")

    # ------------------------------------------------------------------

    def _g(self, key: str, default):
        value = self._config.get(key, default) if self._config is not None else default
        return default if value is None else value

    def _load(self) -> None:
        self._astap_edit.setText(str(self._g("astrometry.astap_path", "") or ""))
        db = str(self._g("astrometry.database", "") or "")
        self._db_combo.setCurrentText(db if db in _ASTAP_DATABASES else "Auto")
        self._radius_spin.setValue(int(self._g("astrometry.search_radius_deg", 30)))
        self._select_downsample(int(self._g("astrometry.downsample", 2)))
        self._grid_spin.setValue(int(self._g("astrometry.grid_spacing_arcmin", 0)))
        self._scale_hint_chk.setChecked(bool(self._g("astrometry.use_scale_hint", True)))
        self._mag_spin.setValue(float(self._g("catalog.mag_limit", 15.0)))
        self._max_spin.setValue(int(self._g("catalog.max_results", 250)))
        self._suspected_chk.setChecked(bool(self._g("catalog.include_suspected", True)))
        self._field_catalogue_chk.setChecked(bool(self._g("catalog.field_stars_enabled", True)))
        self._field_mag_spin.setValue(float(self._g("catalog.field_stars_mag_limit", 18.0)))
        self._field_max_spin.setValue(int(self._g("catalog.field_stars_max_results", 2000)))
        budget = int(self._g("catalog.identification_max_objects", 400))
        budget_index = self._identification_budget.findData(budget)
        self._identification_budget.setCurrentIndex(
            budget_index if budget_index >= 0 else self._identification_budget.findData(400)
        )
        self._field_detected_only_chk.setChecked(
            bool(self._g("catalog.field_stars_detected_only", False))
        )
        self._essential_objects_chk.setChecked(
            bool(self._g("catalog.show_essential_objects", True))
        )
        self._cached_exoplanets_chk.setChecked(
            bool(self._g("catalog.show_cached_exoplanets", True))
        )
        self._named_objects_chk.setChecked(bool(self._g("catalog.named_objects_enabled", True)))
        self._named_max_spin.setValue(int(self._g("catalog.named_objects_max_results", 500)))
        self._apply_identification_budget(self._identification_budget.currentIndex())
        self._named_network_chk.setChecked(
            bool(self._g("catalog.named_objects_allow_network", True))
        )
        self._exoplanet_hosts_chk.setChecked(bool(self._g("catalog.exoplanet_hosts_enabled", True)))
        self._exoplanet_network_chk.setChecked(
            bool(self._g("catalog.exoplanet_hosts_allow_network", True))
        )
        self._select_field_depth()
        self._autocomp_spin.setValue(int(self._g("photometry.auto_comparisons", 5)))
        mode_index = self._recenter_mode.findData(str(self._g("recenter.mode", "off")))
        self._recenter_mode.setCurrentIndex(max(0, mode_index))
        self._recenter_threshold.setValue(float(self._g("recenter.threshold_px", 20.0)))
        self._recenter_every.setValue(int(self._g("recenter.every_n", 30)))
        self._recenter_deadband.setValue(float(self._g("recenter.deadband_px", 2.0)))
        self._recenter_max_step.setValue(float(self._g("recenter.max_step_px", 60.0)))
        self._recenter_settle.setValue(float(self._g("recenter.settle_s", 3.0)))
        self._sync_recenter_enabled()

    def _select_downsample(self, value: int) -> None:
        for i, (_label, v) in enumerate(_DOWNSAMPLE):
            if v == value:
                self._down_combo.setCurrentIndex(i)
                return
        self._down_combo.setCurrentIndex(0)

    def _select_field_depth(self) -> None:
        desired = float(self._field_mag_spin.value())
        for index in range(self._field_depth.count() - 1):
            if self._field_depth.itemData(index) == desired:
                self._field_depth.setCurrentIndex(index)
                return
        self._field_depth.setCurrentIndex(self._field_depth.count() - 1)

    def _apply_field_depth(self, _index: int) -> None:
        preset = self._field_depth.currentData()
        if preset is None:
            return
        self._field_mag_spin.setValue(float(preset))

    def _apply_identification_budget(self, _index: int) -> None:
        """Keep legacy per-provider limits coherent with the shared budget."""
        budget = int(self._identification_budget.currentData() or 400)
        self._field_max_spin.setValue(budget)
        self._named_max_spin.setValue(budget)

    def _browse_astap(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(self, "Select ASTAP binary")
        if path:
            self._astap_edit.setText(path)

    def _on_save(self) -> None:
        if self._config is not None:
            db = self._db_combo.currentText()
            identification_budget = int(self._identification_budget.currentData() or 400)
            self._config.set("astrometry.astap_path", self._astap_edit.text().strip())
            self._config.set("astrometry.database", "" if db == "Auto" else db)
            self._config.set("astrometry.search_radius_deg", int(self._radius_spin.value()))
            self._config.set(
                "astrometry.downsample", _DOWNSAMPLE[self._down_combo.currentIndex()][1]
            )
            self._config.set("astrometry.use_scale_hint", self._scale_hint_chk.isChecked())
            self._config.set("astrometry.grid_spacing_arcmin", int(self._grid_spin.value()))
            self._config.set("catalog.mag_limit", float(self._mag_spin.value()))
            self._config.set("catalog.max_results", int(self._max_spin.value()))
            self._config.set("catalog.include_suspected", self._suspected_chk.isChecked())
            self._config.set("catalog.field_stars_enabled", self._field_catalogue_chk.isChecked())
            self._config.set("catalog.field_stars_mag_limit", float(self._field_mag_spin.value()))
            self._config.set(
                "catalog.identification_max_objects",
                identification_budget,
            )
            self._config.set("catalog.field_stars_max_results", identification_budget)
            self._config.set(
                "catalog.field_stars_detected_only", self._field_detected_only_chk.isChecked()
            )
            self._config.set(
                "catalog.show_essential_objects", self._essential_objects_chk.isChecked()
            )
            self._config.set(
                "catalog.show_cached_exoplanets", self._cached_exoplanets_chk.isChecked()
            )
            self._config.set("catalog.named_objects_enabled", self._named_objects_chk.isChecked())
            self._config.set("catalog.named_objects_max_results", identification_budget)
            self._config.set(
                "catalog.named_objects_allow_network", self._named_network_chk.isChecked()
            )
            self._config.set(
                "catalog.exoplanet_hosts_enabled", self._exoplanet_hosts_chk.isChecked()
            )
            self._config.set(
                "catalog.exoplanet_hosts_allow_network", self._exoplanet_network_chk.isChecked()
            )
            self._config.set("photometry.auto_comparisons", int(self._autocomp_spin.value()))
            self._config.set("recenter.mode", self._recenter_mode.currentData())
            self._config.set("recenter.threshold_px", float(self._recenter_threshold.value()))
            self._config.set("recenter.every_n", int(self._recenter_every.value()))
            self._config.set("recenter.deadband_px", float(self._recenter_deadband.value()))
            self._config.set("recenter.max_step_px", float(self._recenter_max_step.value()))
            self._config.set("recenter.settle_s", float(self._recenter_settle.value()))
            self._config.save()
        self.saved.emit()
        self.accept()
