"""Acquisition mode — the work surface where most session time is spent.

Layout (WS9a — dockable workspace)::

    ┌─ ImageToolbar (View · Open FITS · "display ≠ data") ───────────┐
    ├─ Panel-toggle strip: Camera · Mount · Focuser · Filters · … ───┤
    ├─ OverlayBar: Grid · Stars · Variables · … ────────────────────┤
    │  ┌──────────────────────────────────┬─────────────────────┐   │
    │  │  FitsViewer (hero, central) +    │  Camera dock (right, │   │
    │  │  crosshair + pixel readout       │  Mount/Focuser/Filter│   │
    │  │                                  │  tabbed behind it)   │   │
    │  │  Stats bar: HFD·Stars·Sky·…      │                      │   │
    │  ├──────────────────────────────────┴─────────────────────┤   │
    │  │  Sequence dock (bottom, Log tabbed with it)            │   │
    │  └────────────────────────────────────────────────────────┘   │
    └────────────────────────────────────────────────────────────────┘

Every panel is a real ``QDockWidget`` (movable / resizable / closable /
floatable to a 2nd monitor), hosted in an internal ``QMainWindow`` used as a
plain widget. The central widget is the viewer + the always-visible stats strip.
Layout is persisted via ``QMainWindow.saveState()`` into ``ui.imaging.layout``.

The page is a *view* over the session layer (WS5): the DeviceSession owns the
device handles and pollers, the AcquisitionEngine owns the camera-ownership
state machine and the capture/solve/photometry workers. The page builds the
docks, routes their intents into session/engine methods, and renders the
typed signals coming back.

Upward signals the Shell wires into the global status bar + Connection page
(relays of the session/engine signals, kept here so the Shell wiring and the
tests keep one stable surface):

    device_state_changed(device, state, info)
    tracking_changed(bool | None)
    action_changed(text)
    log_message(level, message)
"""

from __future__ import annotations

import base64
import logging
import math
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QByteArray, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from argos.core.alpaca.telescope import MountPosition
from argos.core.config import Config
from argos.core.session.acquisition_engine import AcquisitionEngine
from argos.core.session.device_session import DeviceSession
from argos.core.session.types import (
    CameraCapabilities,
    FilterWheelState,
    FocuserState,
    LiveFrame,
)
from argos.core.catalog import PointSourceIdentity, cached_exoplanet_hosts_in_cone
from argos.core.catalog.offline import essential_catalogue_objects
from argos.core.catalog.photometry import separation_arcmin
from argos.core.catalog.object_resolver import is_variable_object_type
from argos.core.catalog.targets import TargetStar
from argos.core.hardware import active as hardware
from argos.core.imaging import sensor_models
from argos.core.imaging.astrometry_session import field_geometry, project_points
from argos.core.imaging.debayer import VIEW_SUPERPIXEL
from argos.core.imaging.metrics import (
    arcsec_per_full_px,
    arcsec_per_green_px,
    DEFAULT_STAR_RADIUS,
    TRACK_SNAP_SEARCH,
    measure_star_at,
)
from argos.core.imaging.platesolve import format_dec_dms, format_ra_hms
from argos.core.photometry.airmass import airmass_from_altitude
from argos.ui import design, theme
from argos.ui.panels.log_panel import LogPanel
from argos.ui.panels.manual_control_dialog import ManualControlDialog
from argos.ui.widgets.camera_dock import CameraDock
from argos.ui.widgets.dock_host import make_dock, panel_toolbar_qss, style_toggle_action
from argos.ui.widgets.fits_viewer import FitsViewer
from argos.ui.widgets.focuser_dock import FocuserDock
from argos.ui.widgets.hfd_history_dock import HfdHistoryDock
from argos.ui.widgets.histogram_dock import HistogramDock
from argos.ui.widgets.image_toolbar import ImageToolbar
from argos.ui.widgets.lightcurve_panel import LightCurvePanel
from argos.ui.widgets.mount_dock import MountDock
from argos.ui.panels.photometry_window import PhotometryWindow
from argos.ui.widgets.overlay_bar import OverlayBar
from argos.ui.widgets.astrometry_settings import AstrometrySettingsDialog, SECTION_CATALOG
from argos.ui.widgets.star_info_card import StarInfoCard
from argos.ui.widgets.statistics_dock import StatisticsDock
from argos.workers.camera_service import CameraState
from argos.workers.preview_processor import PreviewProcessor
from argos.workers.object_resolver_worker import (
    NearbyObjectResolverWorker,
    ObjectResolverWorker,
    PointIdentityWorker,
)

logger = logging.getLogger(__name__)

#: Config key for the base64-encoded inner QMainWindow.saveState() blob (WS9a).
_CFG_LAYOUT = "ui.imaging.layout"

#: Camera-ownership state → status-bar action text. The free-text
#: ``action_changed`` strings stay for log/status display only; every
#: enable/disable decision reads :class:`CameraState` from the service.
_STATE_ACTION = {
    CameraState.IDLE: "Idle",
    CameraState.LIVE: "Live preview",
    CameraState.SINGLE: "Taking shot…",
    CameraState.SEQUENCE: "Sequence running",
    CameraState.AUTOFOCUS: "Autofocus running",
}


class ImagingPage(QWidget):
    """The Imaging-mode workspace."""

    device_state_changed = pyqtSignal(str, str, str)  # device, state, info
    tracking_changed = pyqtSignal(object)  # bool | None
    action_changed = pyqtSignal(str)
    log_message = pyqtSignal(str, str)  # level, message
    autofocus_step = pyqtSignal(int, int, int, object)  # step, total, pos, hfd|None
    target_coordinates_changed = pyqtSignal(str, float, float)  # name, RA h, Dec deg
    autofocus_best = pyqtSignal(int, object)  # best position, best hfd|None
    autofocus_state = pyqtSignal(bool)  # sweep running / stopped
    auto_solve_armed = pyqtSignal(bool)  # → the Shell's checkable menu action
    # Capture visibility for the Shell's persistent strip (WS4).
    camera_state_changed = pyqtSignal(object)  # CameraState ownership transitions
    hfd_updated = pyqtSignal(object)  # per-frame HFD, float | None

    def __init__(
        self,
        config: Config,
        session: DeviceSession,
        engine: AcquisitionEngine,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._session = session
        self._engine = engine

        self._processor = PreviewProcessor(self)  # off-thread display compute
        self._jog_dialog: ManualControlDialog | None = None

        self._channel = VIEW_SUPERPIXEL
        self._last_raw: np.ndarray | None = None  # last raw frame, for re-rendering
        self._star_radius = DEFAULT_STAR_RADIUS  # aperture for FWHM (§5)
        self._green_shape: tuple[int, int] | None = None
        self._disp_shape: tuple[int, int] | None = None
        self._selected_green: tuple[float, float] | None = None  # clicked star (green px)
        self._analysis_windows: list = []  # open Open-FITS analysis windows
        # Alias to the engine-owned live plate-solve controller (§6).
        self._astrometry = engine.astrometry
        # Green-px projections of the engine's cached catalog + target set.
        self._var_green: list = []  # parallel to engine.variables (None = off-frame)
        self._visible_variable_indices: set[int] | None = None
        self._field_catalogue_green: list = []  # parallel to engine.field_stars
        self._field_catalogue_names: list = []  # nearest SIMBAD identity, parallel to Gaia
        self._field_catalogue_match_key: tuple | None = None
        self._matched_named_indices: set[int] = set()
        self._point_identities: list[PointSourceIdentity] = []
        self._point_identity_green: list = []
        self._named_objects: list = []
        self._named_objects_green: list = []
        self._deep_sky_objects: list = []
        self._deep_sky_green: list = []
        self._exoplanet_hosts: list = []
        self._exoplanet_green: list = []
        self._display_mag_limit = float(self._config.get("catalog.display_mag_limit", 18.0))
        self._comp_green: list = []
        self._target_green: list = []
        self._detected_green: list[tuple[float, float]] = []
        self._pending_star: dict | None = None  # the clicked star awaiting a role
        self._pending_green: tuple[float, float] | None = None
        self._armed: set = set()  # overlays auto-shown once when first available
        # §6 P4: live photometry preview (light curve + metrics window).
        self._photometry_window: PhotometryWindow | None = None
        self._metrics_t0: float | None = None
        self._batch_worker = None  # WS7 batch re-run (QThread) + its progress dialog
        self._batch_dialog: QProgressDialog | None = None
        self._object_resolver_worker: ObjectResolverWorker | None = None
        self._nearby_object_resolver_worker: NearbyObjectResolverWorker | None = None
        self._point_identity_worker: PointIdentityWorker | None = None
        self._point_identity_request: tuple[float, float, object | None] | None = None
        # Catalogue delivery and live aperture tracking can complete together.
        # Coalesce their overlay work so a large catalogue never monopolises a
        # Qt signal delivery turn (which makes the whole window show the busy
        # cursor, even though the camera/network workers are separate).
        self._catalog_project_timer = QTimer(self)
        self._catalog_project_timer.setSingleShot(True)
        self._catalog_project_timer.timeout.connect(self._project_catalog)

        self._build_ui()
        # The engine reads capture parameters through these providers — the
        # page owns the widgets, the engine never touches them.
        self._engine.set_providers(self._camera_dock.params, self._camera_dock.preview_scale)
        self._wire_signals()
        self._processor.ready.connect(self._on_processed)
        self._processor.start()
        # A persisted target set / pre-existing curves must be visible from
        # the first frame, not only after the next selection or solve.
        self._on_targets_changed(self._engine.target_set())
        self._render_curves()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Display controls (channel / gamma / auto-stretch) sit above the image.
        self._toolbar = ImageToolbar()
        root.addWidget(self._toolbar)

        # Build the control surfaces once; wrapped into docks below.
        self._viewer = FitsViewer()
        # On-image star-info card (bottom-left overlay) for click → info + roles.
        self._info_card = StarInfoCard(self._viewer)
        self._camera_dock = CameraDock()
        self._lightcurve_panel = LightCurvePanel()  # live curve, as a dock (WS7)
        self._mount_dock = MountDock()
        self._focuser_dock = FocuserDock()
        self._histogram_dock = HistogramDock()
        self._statistics_dock = StatisticsDock()
        self._hfd_history_dock = HfdHistoryDock()
        self._log_panel = LogPanel()

        # The workspace is an internal QMainWindow used as a plain widget
        # (Qt.WindowType.Widget) so its whole docking machinery — movable,
        # floatable, tabbed docks — works INSIDE the page without it being a
        # top-level window. The Shell owns the real window.
        self._workspace = QMainWindow()
        self._workspace.setWindowFlags(Qt.WindowType.Widget)
        self._workspace.setDockNestingEnabled(True)
        self._workspace.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.GroupedDragging
        )

        # Central widget: the viewer plus a quiet context strip. Detailed image
        # statistics belong in their optional panel; the always-visible space is
        # reserved for the file and sequence state the observer acts on.
        image_col = QWidget()
        col = QVBoxLayout(image_col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self._viewer, 1)
        col.addWidget(self._build_context_bar())
        self._workspace.setCentralWidget(image_col)

        # Every former panel becomes a real dock. Object names are stable —
        # QMainWindow.saveState()/restoreState() key on them.
        self._docks: dict[str, QDockWidget] = {
            "camera": make_dock("Acquisition", self._camera_dock, object_name="dock.camera"),
            "mount": make_dock("Telescope", self._mount_dock, object_name="dock.mount"),
            "focuser": make_dock("Focusing", self._focuser_dock, object_name="dock.focuser"),
            "display": make_dock("Image display", self._histogram_dock, object_name="dock.display"),
            # Statistics is a compact key/value card (scrolls if the dock is
            # short); HFD History owns an expanding trend plot, so it manages its
            # own layout (scroll=False) and grows with the dock (WS9b).
            "statistics": make_dock(
                "Image statistics", self._statistics_dock, object_name="dock.statistics"
            ),
            "hfd_history": make_dock(
                "Focus diagnostics (HFD/FWHM)",
                self._hfd_history_dock,
                object_name="dock.hfd_history",
                scroll=False,
            ),
            # Log is a wide, self-scrolling panel. (The sequence planner lives
            # in its own Sequencer mode now.)
            "log": make_dock("Activity log", self._log_panel, object_name="dock.log", scroll=False),
            # Live differential light curve — visible during capture without a
            # floating window (WS7). Owns its own plot layout (scroll=False).
            "lightcurve": make_dock(
                "Differential photometry",
                self._lightcurve_panel,
                object_name="dock.lightcurve",
                scroll=False,
            ),
        }

        # Slim panel-toggle strip (NINA's top strip): one checkable chip per
        # dock, wired to the dock's toggleViewAction() — which keeps the check
        # state in sync with real visibility and, unlike visibilityChanged,
        # does NOT fire on tab switches.
        self._panel_bar = self._build_panel_bar()
        root.addWidget(self._panel_bar)

        # Slim overlay-toggle chips under the panel strip (Grid/Stars/…).
        self._overlay_bar = OverlayBar()
        self._overlay_bar.set_magnitude_range(
            float(self._config.get("catalog.field_stars_mag_limit", 18.0))
        )
        self._overlay_bar.set_magnitude_limit(self._display_mag_limit)
        root.addWidget(self._overlay_bar)

        root.addWidget(self._workspace, 1)

        # Apply the sober default arrangement, then a saved layout if present.
        self._apply_default_layout()
        self._restore_layout()

    #: Dock key → scientific observer-facing name.  These names deliberately
    #: describe a task or measurement, never a Qt widget or implementation.
    _PANEL_ORDER = (
        ("camera", "Acquisition"),
        ("mount", "Telescope"),
        ("focuser", "Focusing"),
        ("display", "Image display"),
        ("statistics", "Image statistics"),
        ("hfd_history", "Focus diagnostics (HFD/FWHM)"),
        ("log", "Activity log"),
        ("lightcurve", "Differential photometry"),
    )

    #: Panels used while framing a target.  Secondary diagnostics remain one
    #: click away in the Panels menu instead of turning the cockpit into a row
    #: of jargon before the observer has an image.
    _PRIMARY_PANEL_KEYS = ("camera", "mount", "focuser", "display", "lightcurve")

    def _build_panel_bar(self) -> QToolBar:
        """A slim strip of checkable dock-toggle chips + a Reset-layout action."""
        bar = QToolBar()
        bar.setMovable(False)
        bar.setStyleSheet(panel_toolbar_qss())
        self._panel_actions: dict[str, QAction] = {}
        for key, label in self._PANEL_ORDER:
            action = style_toggle_action(self._docks[key].toggleViewAction(), label)
            action.setToolTip(
                f"Show/hide {label}. Drag its title bar to arrange it; double-click the title "
                "to detach it."
            )
            self._panel_actions[key] = action

        for key in self._PRIMARY_PANEL_KEYS:
            action = self._panel_actions[key]
            bar.addAction(action)

        panels = QToolButton()
        panels.setText("Panels ▾")
        panels.setToolTip("Show optional diagnostics and the activity log")
        panels.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._panels_menu = QMenu(panels)
        for key, _label in self._PANEL_ORDER:
            if key not in self._PRIMARY_PANEL_KEYS:
                self._panels_menu.addAction(self._panel_actions[key])
        panels.setMenu(self._panels_menu)
        bar.addWidget(panels)
        # An explicit layout menu makes the docking model discoverable. Native
        # title-bar dragging remains the fastest path, but the menu gives
        # trackpad users a reliable way to detach panels to a second screen.
        bar.addSeparator()
        arrange = QToolButton()
        arrange.setText("Arrange ▾")
        arrange.setToolTip("Float, re-dock or reset any panel")
        arrange.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._arrange_menu = QMenu(arrange)
        self._arrange_menu.aboutToShow.connect(self._refresh_arrange_menu)
        arrange.setMenu(self._arrange_menu)
        bar.addWidget(arrange)
        reset = bar.addAction("Reset layout")
        reset.setToolTip("Restore the default panel arrangement")
        reset.triggered.connect(self.reset_layout)
        return bar

    def _refresh_arrange_menu(self) -> None:
        """Build the menu from live dock state, so its verbs stay accurate."""
        self._arrange_menu.clear()
        reset = self._arrange_menu.addAction("Restore default layout")
        reset.triggered.connect(self.reset_layout)
        self._arrange_menu.addSeparator()
        for key, label in self._PANEL_ORDER:
            dock = self._docks[key]
            verb = "Dock" if dock.isFloating() else "Float"
            action = self._arrange_menu.addAction(f"{verb} {label}")
            action.triggered.connect(lambda _checked=False, d=dock: self._toggle_dock_floating(d))

    @staticmethod
    def _toggle_dock_floating(dock: QDockWidget) -> None:
        """Float or return a dock while ensuring a hidden dock becomes visible."""
        dock.setFloating(not dock.isFloating())
        dock.show()
        dock.raise_()

    def _build_context_bar(self) -> QWidget:
        """Thin, actionable context below the image rather than raw statistics."""
        bar = QWidget()
        bar.setStyleSheet(f"background:{theme.SURFACE_3}; border-top:1px solid {theme.SURFACE_4};")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 3, 10, 3)
        row.setSpacing(design.SPACING_SM)
        frame_key = design.MutedLabel("Frame")
        frame_key.setMinimumWidth(0)
        row.addWidget(frame_key)
        self._frame_context_lbl = QLabel("No FITS frame loaded")
        self._frame_context_lbl.setStyleSheet(
            f"color:{theme.FG_MUTED}; font-size:{design.FONT_SIZE_SMALL}px;"
            f" background:transparent; font-family:{theme.FONT_MONO};"
        )
        self._frame_context_lbl.setToolTip("Most recently opened or saved FITS image")
        row.addWidget(self._frame_context_lbl, 1)
        self._sequence_context_lbl = QLabel("")
        self._sequence_context_lbl.setStyleSheet(
            f"color:{theme.FG}; font-size:{design.FONT_SIZE_SMALL}px;"
            f" background:transparent; font-family:{theme.FONT_MONO};"
        )
        self._sequence_context_lbl.setToolTip("Progress of the active acquisition sequence")
        row.addWidget(self._sequence_context_lbl)
        row.addStretch()
        return bar

    # ------------------------------------------------------------------
    # Dockable workspace layout (WS9a)
    # ------------------------------------------------------------------

    def _apply_default_layout(self) -> None:
        """Sober-by-default arrangement (the docks live in fixed home areas).

        Camera right; Mount / Focuser / Filter wheel tabbed behind it; Sequence
        bottom with Log tabbed behind it; Display / Statistics / HFD History
        hidden (right-area homes, summoned on demand). Called on first launch and
        by "Reset layout".
        """
        hidden = ("display", "statistics", "hfd_history", "lightcurve")
        w = self._workspace
        right = Qt.DockWidgetArea.RightDockWidgetArea
        bottom = Qt.DockWidgetArea.BottomDockWidgetArea

        # Detach everything first so a reset is idempotent.
        for dock in self._docks.values():
            w.removeDockWidget(dock)

        w.addDockWidget(right, self._docks["camera"])
        w.addDockWidget(right, self._docks["mount"])
        w.addDockWidget(right, self._docks["focuser"])
        # Mount / Focuser share Camera's stack, tabbed behind it. (The filter
        # wheel has no dock: its slots are driven from the Camera form and the
        # sequence rows — the wheel is capture state, not a device to babysit.)
        for key in ("mount", "focuser"):
            w.tabifyDockWidget(self._docks["camera"], self._docks[key])
        self._docks["camera"].raise_()

        # Log anchors the bottom; the light curve tabs behind it (summoned on
        # demand during a run). The sequence planner has its own mode now.
        w.addDockWidget(bottom, self._docks["log"])
        w.addDockWidget(bottom, self._docks["lightcurve"])
        w.tabifyDockWidget(self._docks["log"], self._docks["lightcurve"])
        self._docks["log"].raise_()

        # Display / Statistics / HFD History have right-area homes but start
        # hidden — docks the user summons on demand from the panel strip.
        for key in hidden:
            w.addDockWidget(right, self._docks[key])

        for key, dock in self._docks.items():
            dock.setVisible(key not in hidden)

    def _restore_layout(self) -> None:
        """Restore a persisted layout over the defaults, if one exists."""
        blob = self._config.get(_CFG_LAYOUT)
        if not blob:
            return
        try:
            self._workspace.restoreState(QByteArray(base64.b64decode(blob)))
        except Exception as exc:  # noqa: BLE001 — a corrupt blob must not crash startup
            logger.warning("Imaging layout restore failed: %s", exc)

    def save_layout(self) -> None:
        """Persist the current dock arrangement (called by the Shell on close)."""
        state = bytes(self._workspace.saveState())
        self._config.set(_CFG_LAYOUT, base64.b64encode(state).decode())

    def reset_layout(self) -> None:
        """Clear the saved layout and restore the sober defaults, live."""
        self._config.set(_CFG_LAYOUT, None)
        self._apply_default_layout()

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _wire_signals(self) -> None:
        # Device session → upward relays (status bar via the Shell), docks
        # and the typed device-state slots.
        s = self._session
        s.device_state_changed.connect(self._on_session_device_state)
        s.tracking_changed.connect(self.tracking_changed)
        s.action_changed.connect(self.action_changed)
        s.log_message.connect(self.log_message)
        s.mount_position.connect(self._on_mount_position)
        s.capabilities_ready.connect(self._on_camera_capabilities)
        s.filterwheel_state.connect(self._on_filterwheel_state)
        s.focuser_state.connect(self._on_focuser_state)
        s.filter_moved.connect(self._on_filter_moved)
        s.camera_temperature.connect(self._camera_dock.set_temperature)
        s.focuser_temperature.connect(self._focuser_dock.set_temperature)
        s.slewed.connect(self._clear_astrometry)  # a goto stales the solve

        # Acquisition engine → upward relays, docks and the frame pipeline.
        e = self._engine
        e.camera_state_changed.connect(self._on_camera_state)
        e.camera_state_changed.connect(self.camera_state_changed)
        e.device_state_changed.connect(self.device_state_changed)  # camera busy/idle
        e.log_message.connect(self.log_message)
        e.action_changed.connect(self.action_changed)
        e.frame_ready.connect(self._on_frame)
        e.sequence_running.connect(self._on_sequence_running)  # auto-solve arming
        e.sequence_progress.connect(self._on_sequence_progress)
        e.frame_saved.connect(self._on_seq_frame_saved)
        e.autofocus_state.connect(self._on_autofocus_state)
        e.autofocus_step.connect(self._on_af_step)
        e.autofocus_best.connect(self._on_af_done)
        e.catalog_ready.connect(self._on_catalog_ready)
        e.targets_changed.connect(self._on_targets_changed)
        e.photometry_measuring.connect(self._on_photometry_measuring)
        e.photometry_point.connect(self._on_photometry_point)
        e.curves_changed.connect(self._render_curves)  # dock + window, one store
        e.comparison_quality_updated.connect(self._on_comparison_quality_updated)
        e.apertures_tracked.connect(self._schedule_catalog_project)  # markers follow the frame
        # Live plate-solve controller (engine-owned, shared pipeline).
        self._astrometry.solved.connect(self._on_astrometry_solved)

        # Toolbar (view + Open FITS only — solve/photometry actions live in
        # the Shell's Astrometry / Photometry menus).
        self._toolbar.channel_changed.connect(self._on_channel_changed)
        self._toolbar.open_requested.connect(self._on_open_fits)
        # Overlay chips + the on-image star-info card.
        self._overlay_bar.toggled.connect(self._on_overlay_toggled)
        self._overlay_bar.configure_requested.connect(self._open_field_catalogue)
        self._overlay_bar.magnitude_changed.connect(self._on_catalogue_magnitude_changed)
        self._overlay_bar.magnitude_committed.connect(self._save_catalogue_magnitude)
        self._info_card.role_selected.connect(self._on_card_role)
        self._info_card.remove_selected.connect(self._on_card_remove)
        self._info_card.cleared.connect(self._on_card_cleared)
        # Display pipeline: the Display tab (histogram/stretch) ↔ the viewer.
        self._histogram_dock.stretch_changed.connect(self._viewer.set_stretch)
        self._histogram_dock.auto_requested.connect(self._viewer.auto_stretch)
        self._histogram_dock.saturation_toggled.connect(self._on_saturation_toggled)
        self._histogram_dock.roi_toggled.connect(self._viewer.set_roi_enabled)
        self._histogram_dock.crosshair_toggled.connect(self._viewer.set_crosshair_enabled)
        self._histogram_dock.loupe_toggled.connect(self._viewer.set_loupe_enabled)
        self._histogram_dock.astrometry_toggled.connect(self._viewer.set_astrometry_enabled)
        self._histogram_dock.star_radius_changed.connect(self._on_star_radius)
        self._histogram_dock.rotation_changed.connect(self._on_rotation_changed)
        # Display rotation: default Auto = portrait sensor shown landscape.
        rotation = str(self._config.get("ui.display.rotation", "auto") or "auto")
        self._viewer.set_rotation(rotation)
        self._histogram_dock.set_rotation_mode(rotation)
        self._viewer.star_clicked.connect(self._on_star_clicked)
        self._viewer.levels_changed.connect(self._histogram_dock.set_levels)
        self._viewer.region_info.connect(self._histogram_dock.set_region_info)

        # Camera dock — capture intents go straight to the engine.
        self._camera_dock.take_shot_clicked.connect(self._engine.take_shot)
        self._camera_dock.live_start_requested.connect(self._engine.start_live)
        self._camera_dock.live_stop_requested.connect(self._engine.stop_live)
        self._camera_dock.offset_changed.connect(self._on_camera_offset)
        self._camera_dock.binning_changed.connect(self._on_camera_binning)
        self._camera_dock.filter_selected.connect(self._on_camera_dock_filter)
        # Object edits re-key the target set + curves: every view must follow.
        self._camera_dock.object_changed.connect(self._engine.on_object_changed)

        # Filter wheel dock

        # Mount dock — command intents go straight to the device session.
        self._mount_dock.goto_clicked.connect(self._on_goto)
        self._mount_dock.object_lookup_requested.connect(self._lookup_object)
        self._mount_dock.resolved_goto_clicked.connect(self._goto_resolved_object)
        self._mount_dock.target_suggestion_accepted.connect(self.set_catalogue_target)
        self._mount_dock.sync_to_current_clicked.connect(self._session.sync_current)
        self._mount_dock.center_target_clicked.connect(self._center_selected_target)
        self._mount_dock.tracking_toggled.connect(self._session.set_tracking)
        self._mount_dock.tracking_rate_changed.connect(self._session.set_tracking_rate)
        self._mount_dock.abort_clicked.connect(self._session.abort_slew)
        self._mount_dock.park_clicked.connect(self._session.park)
        self._mount_dock.manual_control_requested.connect(self._open_jog)
        self._mount_dock.jog_start.connect(self._session.jog)
        self._mount_dock.jog_stop.connect(self._on_jog_stop)

        # Focuser dock
        self._focuser_dock.step_requested.connect(self._on_focuser_step)
        self._focuser_dock.halt_requested.connect(self._on_focuser_halt)
        self._focuser_dock.autofocus_requested.connect(self._engine.start_autofocus)
        self._focuser_dock.move_to_requested.connect(self._on_focuser_move_to)

        # Logs reach the bottom log panel locally + propagate up to the Shell.
        self.log_message.connect(self._log_panel.append)

    # ------------------------------------------------------------------
    # Camera-ownership state (WS3) — one source of truth for guards
    # ------------------------------------------------------------------

    @property
    def camera_service(self):
        """The engine's ownership state machine (widgets may subscribe)."""
        return self._engine.camera_service

    @property
    def camera_state(self) -> CameraState:
        """Current camera owner — the value widgets base enable/disable on."""
        return self._engine.camera_state

    @pyqtSlot(object)
    def _on_camera_state(self, state: CameraState) -> None:
        """Reflect ownership transitions in the status bar + the Live toggle."""
        self.action_changed.emit(_STATE_ACTION[state])
        self._camera_dock.set_live_running(state is CameraState.LIVE)
        # WS8 cockpit: freeze the capture form while the sequence owns the
        # camera (SEQUENCE and its nested AUTOFOCUS pass); any release —
        # complete, stopped or error — transitions state and unfreezes.
        self._camera_dock.set_sequence_lock(state in (CameraState.SEQUENCE, CameraState.AUTOFOCUS))

    # ------------------------------------------------------------------
    # Device session state → docks (the session owns the device handles)
    # ------------------------------------------------------------------

    @pyqtSlot(str, str, str)
    def _on_session_device_state(self, device: str, state: str, info: str) -> None:
        """Reflect a device transition in the docks, then relay it upward."""
        enabled = state in ("connected", "busy")
        if device == "mount":
            self._mount_dock.set_enabled(enabled)
        elif device == "camera":
            self._camera_dock.set_enabled(enabled)
            if state == "disconnected":
                self._camera_dock.reset_camera_limits()
        elif device == "focuser":
            self._focuser_dock.set_enabled(enabled)
            if state == "disconnected":
                self._focuser_dock.set_temperature(None)
        self.device_state_changed.emit(device, state, info)

    @pyqtSlot(object)
    def _on_camera_capabilities(self, caps: CameraCapabilities) -> None:
        """Push driver-derived limits into the capture forms and expose the
        optional parameters (offset / binning) the driver proves it supports."""
        self._camera_dock.set_gain_range(caps.gain_min, caps.gain_max)
        self._camera_dock.set_exposure_range(caps.exposure_min, caps.exposure_max)
        if caps.offset is not None and caps.offset_min is not None and caps.offset_max is not None:
            self._camera_dock.set_offset_support(caps.offset_min, caps.offset_max, caps.offset)
        if caps.max_bin > 1:
            self._camera_dock.set_binning_support(caps.max_bin, caps.binning)

    @pyqtSlot(object)
    def _on_filterwheel_state(self, fw: FilterWheelState) -> None:
        # The wheel's real slot names become THE filter vocabulary everywhere
        # a filter is picked (camera form here, sequence rows on the Sequencer
        # page) — one source, so a picked name always resolves to a position.
        names = list(fw.names)
        self._camera_dock.set_filter_options(names)
        self._camera_dock.set_current_filter(fw.position_name)

    @pyqtSlot(object)
    def _on_focuser_state(self, foc: FocuserState) -> None:
        self._focuser_dock.set_position(foc.position)

    def _on_filter_move(self, position: int) -> None:
        if not self._session.filterwheel:
            return
        self._camera_dock.set_filter_moving(True)
        self._session.move_filter(position)

    def _on_camera_dock_filter(self, name: str) -> None:
        """The CameraDock filter combo physically moves the wheel — the FITS
        metadata must describe the filter that was really in front of the sensor."""
        if not self._session.filterwheel:
            self.log_message.emit(
                "WARN", f"Filter wheel not connected — '{name}' is metadata only."
            )
            return
        position = self._session.filter_position_for(name)
        if position is None:
            self.log_message.emit("WARN", f"No wheel position matches filter '{name}'.")
            return
        self._on_filter_move(position)

    @pyqtSlot(int, str)
    def _on_filter_moved(self, _position: int, name: str) -> None:
        # Keep the CameraDock combo (single-shot metadata) on the real position.
        self._camera_dock.set_filter_moving(False)
        self._camera_dock.set_current_filter(name)

    # ------------------------------------------------------------------
    # Camera actions
    # ------------------------------------------------------------------

    def _on_channel_changed(self, channel: str) -> None:
        self._channel = channel
        # Re-render the last frame (via the worker) so the view switch is visible
        # even when no live preview is running.
        if self._last_raw is not None:
            self._processor.submit(self._last_raw, channel)

    def _on_saturation_toggled(self, enabled: bool) -> None:
        threshold = int(self._config.get("camera.full_well_adu", 60000))
        self._viewer.set_saturation(enabled, threshold)

    def _on_rotation_changed(self, mode: str) -> None:
        self._viewer.set_rotation(mode)
        self._config.set("ui.display.rotation", mode)  # saved by the Shell on close

    def _show_raw(self, full_arr) -> None:
        """Submit a raw frame to the preview worker (heavy compute off-thread)."""
        self._last_raw = full_arr
        self._processor.submit(full_arr, self._channel)

    @pyqtSlot(object)
    def _on_processed(self, pf) -> None:
        """Apply a worker-processed frame to the UI (cheap work, UI thread)."""
        if self._green_shape is not None and pf.green_shape != self._green_shape:
            # The green coordinate frame changed size (preview quality toggle,
            # binning) — a click selection from the old frame is stale.
            self._selected_green = None
            self._viewer.clear_selection()
            self._info_card.hide()
        self._green_shape = pf.green_shape
        self._disp_shape = pf.display.shape[:2]
        self._camera_dock.set_hfd(pf.metrics.hfd)
        self._hfd_history_dock.push_metrics(pf.metrics)  # trend panel (WS9b)
        self._statistics_dock.set_frame(pf)  # full stats grid (WS9b)
        self.hfd_updated.emit(pf.metrics.hfd)  # Shell capture strip
        # Histogram first: sets the slider/data range, then the viewer's
        # auto-stretch emits levels that the dock sliders sync to.
        self._histogram_dock.set_histogram(pf.centers, pf.r, pf.g, pf.b, pf.lo, pf.hi)
        self._viewer.set_frame_geometry(pf.green_shape)
        self._viewer.display(pf.display)
        self._detected_green = [(float(star.x), float(star.y)) for star in pf.stars.stars]
        # Keep a clicked star's FWHM readout live as new frames arrive.
        self._remeasure_selection()
        self._feed_metrics(pf)  # session metrics (when the photometry window is open)
        # WCS geometry guard: the grid + catalog markers are projected in the
        # *solved* frame's green px. If the displayed frame no longer matches
        # (preview quality toggle, binning change), they would draw at the
        # wrong positions and clicks would mis-identify stars — drop them,
        # same policy as a slew. Re-solving at full res restores everything.
        if (
            self._astrometry.wcs is not None
            and self._astrometry.green_shape is not None
            and self._astrometry.green_shape != pf.green_shape
        ):
            self._clear_astrometry()
            self.log_message.emit(
                "INFO", "Frame geometry changed — WCS overlays cleared (solve again at full res)."
            )
        # Per-frame engine bookkeeping + the auto-solve policy (the engine
        # skips half-quality previews — their plate scale would poison a solve).
        self._engine.on_processed(pf.green_shape, pf.metrics, pf.stars.mean_fwhm)
        if self._astrometry.wcs is not None:
            # The detected-source set is frame-specific, so use it immediately
            # to distinguish a validated marker from an empty-sky coordinate.
            self._schedule_catalog_project()
        if self._photometry_window is not None and self._photometry_window.isVisible():
            self._sync_photometry_setup()

    # ------------------------------------------------------------------
    # Star measurement on click (§5)
    # ------------------------------------------------------------------

    def _on_star_radius(self, radius: int) -> None:
        """User changed the FWHM aperture — re-render overlay + selection."""
        self._star_radius = max(2, int(radius))
        self._processor.set_radius(self._star_radius)
        if self._last_raw is not None:
            self._processor.submit(self._last_raw, self._channel)  # refresh overlay

    def _on_star_clicked(self, x_disp: float, y_disp: float) -> None:
        """Hit-test the click (target → variable → comparison → field star) and
        show the on-image info card with role actions."""
        gp = self._disp_to_green(x_disp, y_disp)
        if gp is None or self._last_raw is None:
            return
        gx, gy = gp
        # 1) a saved target. Identification must not depend on an overlay
        # being visible: hiding markers changes presentation, never data.
        i = self._nearest(self._target_green, gx, gy)
        if i is not None:
            s = self._engine.target_set().stars[i]
            self._present_card(
                self._target_green[i],
                f"Target · {s.display_name}",
                self._target_body(s),
                dict(
                    ra_deg=s.ra_deg,
                    dec_deg=s.dec_deg,
                    auid=s.auid,
                    name=s.name,
                    source=s.source,
                    mags=dict(s.mags),
                ),
            )
            return
        # 2) a VSX variable
        i = self._nearest(self._var_green, gx, gy)
        if i is not None:
            v = self._engine.variables[i]
            self._present_card(
                self._var_green[i],
                f"Variable · {v.name}",
                self._variable_body(v),
                dict(
                    ra_deg=v.ra_deg,
                    dec_deg=v.dec_deg,
                    auid=v.auid,
                    name=v.name,
                    source="vsx",
                    mags={},
                ),
            )
            return
        # 3) an AAVSO VSP comparison (again, independent of marker visibility).
        i = self._nearest(self._comp_green, gx, gy)
        if i is not None:
            c = self._engine.comparisons[i]
            self._present_card(
                self._comp_green[i],
                f"VSP reference · {c.auid}",
                self._comparison_body(c),
                dict(
                    ra_deg=c.ra_deg,
                    dec_deg=c.dec_deg,
                    auid=c.auid,
                    name=c.auid,
                    source="vsp",
                    mags={b.band: b.mag for b in c.bands},
                    chart_id=c.chart_id,
                ),
            )
            return
        # 4) SIMBAD name/type information for the solved field.
        i = self._nearest(self._named_objects_green, gx, gy)
        if i is not None:
            item = self._named_objects[i]
            self._present_card(
                self._named_objects_green[i],
                f"Identified object · {item.name}",
                self._named_object_tooltip(item),
                dict(
                    ra_deg=item.ra_deg,
                    dec_deg=item.dec_deg,
                    auid=None,
                    name=item.name,
                    source="simbad",
                    mags=dict(getattr(item, "mags", ())),
                ),
            )
            return
        # 5) local deep-sky context (Messier/NGC/IC).
        i = self._nearest(self._deep_sky_green, gx, gy)
        if i is not None:
            item = self._deep_sky_objects[i]
            self._present_card(
                self._deep_sky_green[i],
                f"Deep-sky object · {item.name}",
                self._deep_sky_tooltip(item),
                dict(
                    ra_deg=item.ra_degrees,
                    dec_deg=item.dec_degrees,
                    auid=None,
                    name=item.name,
                    source="argos_essential",
                    mags={"V": item.magnitude} if item.magnitude is not None else {},
                ),
            )
            return
        # 6) an exoplanet host from NASA (or its field cache).
        i = self._nearest(self._exoplanet_green, gx, gy)
        if i is not None:
            host = self._exoplanet_hosts[i]
            self._present_card(
                self._exoplanet_green[i],
                f"Exoplanet host · {host.host_name}",
                self._exoplanet_tooltip(host),
                dict(
                    ra_deg=host.ra_degrees,
                    dec_deg=host.dec_degrees,
                    auid=None,
                    name=host.host_name,
                    source="nasa_exoplanet_cache",
                    mags=dict(getattr(host, "mags", ())),
                ),
            )
            return
        # 7) a named Gaia source.
        i = self._nearest(self._field_catalogue_green, gx, gy)
        if i is not None:
            star = self._engine.field_stars[i]
            identity = self._field_catalogue_names[i]
            magnitudes = {
                band: value
                for band, value in (
                    ("G", star.g_mag),
                    ("BP", star.bp_mag),
                    ("RP", star.rp_mag),
                )
                if value is not None
            }
            self._present_card(
                self._field_catalogue_green[i],
                (
                    f"Identified star · {identity.name}"
                    if identity is not None
                    else f"Gaia DR3 · {star.source_id}"
                ),
                self._field_catalogue_body(star, identity),
                dict(
                    ra_deg=star.ra_deg,
                    dec_deg=star.dec_deg,
                    auid=None,
                    name=identity.name if identity is not None else star.display_name,
                    source="gaia_dr3+simbad" if identity is not None else "gaia_dr3",
                    mags=magnitudes,
                ),
            )
            return
        # 8) an identity acquired by an earlier point lookup.  These markers
        # complement the budgeted field list and remain available while the
        # current field is tracked.
        i = self._nearest(self._point_identity_green, gx, gy)
        if i is not None:
            identity = self._point_identities[i]
            self._present_point_identity(identity, self._point_identity_green[i])
            return

        # 9) an unmarked field source.  Measure/snap its image centroid, then
        # run a tiny Gaia/SIMBAD cone query off the UI thread.  If no catalogue
        # match exists, retain the current manual-photometry fallback.
        meas = measure_star_at(self._last_raw, gx, gy, self._star_radius)
        if self._astrometry.wcs is not None:
            px = meas.x if meas is not None else gx
            py = meas.y if meas is not None else gy
            self._start_point_identification(px, py, meas)
            return
        if meas is None:
            self._viewer.clear_selection()
            self._info_card.hide()
            self._selected_green = None
            self._pending_star = None
            return
        self._selected_green = (meas.x, meas.y)
        self._present_field_card(meas)

    def _nearest(self, positions, gx: float, gy: float) -> int | None:
        """Index of the marker nearest (gx, gy) green px within tolerance."""
        if not positions:
            return None
        tol = 10.0
        if self._green_shape and self._disp_shape and self._disp_shape[1] > 0:
            tol = max(6.0, 14.0 * self._green_shape[1] / self._disp_shape[1])
        best_i, best_d = None, tol
        for i, p in enumerate(positions):
            if p is None:
                continue
            d = ((p[0] - gx) ** 2 + (p[1] - gy) ** 2) ** 0.5
            if d <= best_d:
                best_i, best_d = i, d
        return best_i

    def _present_card(self, green_pos, title: str, body: str, pending: dict) -> None:
        """Ring a catalog/target pick and show its info card (roles enabled)."""
        self._selected_green = None  # a catalog pick, not a measured field star
        self._pending_star = pending
        self._pending_green = (float(green_pos[0]), float(green_pos[1]))
        dp = self._green_to_disp(green_pos[0], green_pos[1])
        aperture, _annulus_in, _annulus_out = self._engine.photometry_geometry()
        if dp is not None:
            self._viewer.mark_selection(
                dp[0], dp[1], "", self._green_len_to_disp(aperture), show_label=False
            )
        # Remove is offered iff the clicked star is already in the target set —
        # symmetric with adding it from the image.
        key = TargetStar(role="target", **pending).key()
        saved = {s.key() for s in self._engine.target_set().stars}
        self._info_card.show_star(
            title,
            f"{body}\nPhotometry aperture {aperture:.1f} green px",
            roles_enabled=True,
            removable=key in saved,
        )
        self._info_card.reposition()

    def _present_field_card(self, meas, *, catalogue_note: str = "") -> None:
        """Ring a measured field star; role buttons need a solve for its RA/Dec."""
        wcs = self._astrometry.wcs
        pending = None
        if wcs is not None:
            ra_h, dec_d = wcs.pixel_to_radec(meas.x, meas.y)
            pending = dict(
                ra_deg=ra_h * 15.0, dec_deg=dec_d, auid=None, name=None, source="manual", mags={}
            )
        self._pending_star = pending
        self._pending_green = None
        dp = self._green_to_disp(meas.x, meas.y)
        if dp is not None:
            self._viewer.mark_selection(
                dp[0], dp[1], "", self._green_len_to_disp(meas.radius), show_label=False
            )
        title = "Manual image measurement"
        identity_line = (
            catalogue_note
            or "No loaded catalogue marker. Gaia/SIMBAD point identification is available."
        )
        self._info_card.show_star(
            title,
            f"{identity_line}\n" + self._format_star_text(meas),
            roles_enabled=pending is not None,
        )
        self._info_card.reposition()

    def _start_point_identification(self, gx: float, gy: float, meas) -> None:
        """Identify one unmarked click using a narrow, cache-backed cone."""
        if self._point_identity_worker is not None:
            self.log_message.emit(
                "INFO", "Point identification already in progress; wait for its result."
            )
            return
        wcs = self._astrometry.wcs
        if wcs is None:
            if meas is not None:
                self._present_field_card(meas)
            return
        ra_h, dec_d = wcs.pixel_to_radec(gx, gy)
        self._point_identity_request = (float(gx), float(gy), meas)
        self._pending_star = None
        self._pending_green = None
        dp = self._green_to_disp(gx, gy)
        aperture, _annulus_in, _annulus_out = self._engine.photometry_geometry()
        if dp is not None:
            self._viewer.mark_selection(
                dp[0], dp[1], "", self._green_len_to_disp(aperture), show_label=False
            )
        self._info_card.show_star(
            "Identifying catalogue source…",
            f"Point lookup within 10″\nRA {format_ra_hms(ra_h)}  Dec {format_dec_dms(dec_d)}\n"
            "Gaia astrometry · SIMBAD identity",
            roles_enabled=False,
        )
        self._info_card.reposition()

        use_gaia = bool(self._cfg("catalog.field_stars_enabled", True))
        use_simbad = bool(self._cfg("catalog.named_objects_enabled", True))
        worker = PointIdentityWorker(
            ra_h * 15.0,
            dec_d,
            use_gaia=use_gaia,
            use_simbad=use_simbad,
            allow_gaia_network=use_gaia,
            allow_simbad_network=use_simbad
            and bool(self._cfg("catalog.named_objects_allow_network", True)),
            parent=self,
        )
        self._point_identity_worker = worker
        worker.resolved.connect(self._on_point_identity_resolved)
        worker.failed.connect(self._on_point_identity_failed)
        worker.finished.connect(self._finish_point_identification)
        worker.start()

    @pyqtSlot(object)
    def _on_point_identity_resolved(self, identity) -> None:
        request = self._point_identity_request
        if request is None:
            return
        gx, gy, meas = request
        if identity is None:
            if meas is not None:
                self._present_field_card(
                    meas,
                    catalogue_note="No Gaia or SIMBAD source matched within 10″.",
                )
            else:
                self._viewer.clear_selection()
                self._info_card.show_star(
                    "No catalogue match",
                    "No measured stellar centroid or Gaia/SIMBAD identity was found within 10″.",
                    roles_enabled=False,
                )
                self._info_card.reposition()
            self.log_message.emit("INFO", "Point identification: no catalogue match within 10″.")
            return

        existing = next(
            (
                item
                for item in self._point_identities
                if separation_arcmin(item.ra_deg, item.dec_deg, identity.ra_deg, identity.dec_deg)
                <= 1.0 / 60.0
            ),
            None,
        )
        if existing is None:
            self._point_identities.append(identity)
        else:
            identity = existing
        self._schedule_catalog_project()
        try:
            index = self._point_identities.index(identity)
            green_pos = self._point_identity_green[index]
        except (ValueError, IndexError):
            green_pos = None
        self._present_point_identity(identity, green_pos or (gx, gy))
        self.log_message.emit(
            "OK",
            f"Point identified: {identity.name} ({identity.source}, "
            f"offset {identity.separation_arcsec:.1f}″).",
        )

    @pyqtSlot(str)
    def _on_point_identity_failed(self, message: str) -> None:
        request = self._point_identity_request
        if request is not None and request[2] is not None:
            self._present_field_card(
                request[2], catalogue_note="Point catalogue lookup unavailable."
            )
        elif request is not None:
            self._viewer.clear_selection()
            self._info_card.show_star(
                "Point identification unavailable",
                message,
                roles_enabled=False,
            )
            self._info_card.reposition()
        self.log_message.emit("WARN", f"Point identification: {message}")

    def _finish_point_identification(self) -> None:
        worker = self._point_identity_worker
        self._point_identity_worker = None
        self._point_identity_request = None
        if worker is not None:
            worker.deleteLater()

    def _present_point_identity(self, identity: PointSourceIdentity, green_pos) -> None:
        category = self._object_category(identity.object_type)
        title_kind = "star" if category == "star" else "object"
        lines = [
            f"Source  {identity.source}",
            f"Object type  {identity.object_type or '—'}",
        ]
        if identity.gaia_source_id:
            lines.append(f"Gaia DR3 source  {identity.gaia_source_id}")
        lines.extend(f"{band} magnitude  {value:.3f}" for band, value in identity.mags)
        lines.extend(
            (
                f"RA {format_ra_hms(identity.ra_deg / 15.0)}  "
                f"Dec {format_dec_dms(identity.dec_deg)}",
                f"WCS match offset  {identity.separation_arcsec:.1f}″",
            )
        )
        self._present_card(
            green_pos,
            f"Identified {title_kind} · {identity.name}",
            "\n".join(lines),
            dict(
                ra_deg=identity.ra_deg,
                dec_deg=identity.dec_deg,
                auid=None,
                name=identity.name,
                source="point_catalogue",
                mags=dict(identity.mags),
            ),
        )

    def _variable_body(self, v) -> str:
        lines = []
        if v.var_type:
            lines.append(f"type {v.var_type}" + ("  (suspected)" if v.is_suspected else ""))
        rng = []
        if v.max_mag:
            rng.append(f"max {v.max_mag}")
        if v.min_mag and v.min_mag != "?":
            rng.append(f"min {v.min_mag}")
        if rng:
            lines.append("  ".join(rng))
        if v.period:
            lines.append(f"period {v.period:g} d")
        if v.auid:
            lines.append(f"AUID {v.auid}")
        lines.append(f"RA {format_ra_hms(v.ra_deg / 15.0)}  Dec {format_dec_dms(v.dec_deg)}")
        return "\n".join(lines)

    def _comparison_body(self, c) -> str:
        lines = [f"AAVSO VSP reference  {c.auid}"]
        gaia_matches = [
            star
            for star in self._engine.field_stars
            if separation_arcmin(c.ra_deg, c.dec_deg, star.ra_deg, star.dec_deg) <= 0.05
        ]
        if gaia_matches:
            lines.append(f"Gaia DR3 source  {gaia_matches[0].source_id}")
        mags = [f"{b.band} {b.mag:.3f}" for b in c.bands]
        if mags:
            lines.append("  ".join(mags))
        if c.label:
            lines.append(f"VSP chart code {c.label}  (magnitude code, not a name)")
        lines.append(f"RA {format_ra_hms(c.ra_deg / 15.0)}  Dec {format_dec_dms(c.dec_deg)}")
        return "\n".join(lines)

    def _target_body(self, s) -> str:
        lines = [f"role {s.role}  ·  source {s.source}"]
        if s.mags:
            lines.append("  ".join(f"{b} {m:.3f}" for b, m in s.mags.items()))
        lines.append(f"RA {format_ra_hms(s.ra_deg / 15.0)}  Dec {format_dec_dms(s.dec_deg)}")
        return "\n".join(lines)

    def _remeasure_selection(self) -> None:
        """Re-measure the pinned star (new frame / radius change), centre stable."""
        if self._selected_green is None or self._last_raw is None:
            return
        # Tight snap → the centre stays put when the aperture radius changes; it
        # only tracks small frame-to-frame drift.
        meas = measure_star_at(
            self._last_raw,
            self._selected_green[0],
            self._selected_green[1],
            self._star_radius,
            search=TRACK_SNAP_SEARCH,
        )
        if meas is None:
            return
        self._selected_green = (meas.x, meas.y)  # follow small tracking drift
        self._show_selection(meas)

    def _show_selection(self, meas) -> None:
        # Keep the tracked field star's ring following small drift; the info is in
        # the on-image card (drawn once on click), so suppress the viewer label.
        dp = self._green_to_disp(meas.x, meas.y)
        if dp is None:
            return
        radius_disp = self._green_len_to_disp(meas.radius)
        self._viewer.mark_selection(dp[0], dp[1], "", radius_disp, show_label=False)

    def _format_star_text(self, meas) -> str:
        parts = ["Selected star"]
        if meas.fwhm is not None:
            parts.append(f"FWHM {meas.fwhm * arcsec_per_green_px():.1f}″")
        if meas.hfd is not None:
            parts.append(f"HFD {meas.hfd * arcsec_per_green_px():.1f}″")
        if meas.eccentricity is not None:
            parts.append(f"ecc {meas.eccentricity:.2f}")
        parts.append(f"SNR {meas.snr:.0f}")
        parts.append(f"peak {meas.peak_adu} ADU")
        line1 = "   ".join(parts)
        # Frame astrometry: pointing from the mount + plate scale (no solve yet).
        pos = self._session.last_position
        if pos is not None:
            line2 = (
                f"field  RA {pos.ra:.3f}h  Dec {pos.dec:+.2f}°   ·   {arcsec_per_full_px():.2f}″/px"
            )
        else:
            line2 = f"scale  {arcsec_per_full_px():.2f}″/px   (mount not connected)"
        text = f"{line1}\n{line2}"
        wcs = self._astrometry.wcs
        if wcs is not None:  # plate-solved → the star's true celestial position
            ra_h, dec_d = wcs.pixel_to_radec(meas.x, meas.y)
            text += f"\nstar   RA {format_ra_hms(ra_h)}  Dec {format_dec_dms(dec_d)}"
            text += "\ncatalogue identifier   no loaded marker"
        else:
            text += "\ncatalogue identifier   identify the field first"
        return text

    def _disp_to_green(self, x_disp: float, y_disp: float) -> tuple[float, float] | None:
        if self._green_shape is None or self._disp_shape is None:
            return None
        gh, gw = self._green_shape
        dh, dw = self._disp_shape
        if dw <= 0 or dh <= 0:
            return None
        return x_disp * gw / dw, y_disp * gh / dh

    def _green_to_disp(self, x_green: float, y_green: float) -> tuple[float, float] | None:
        if self._green_shape is None or self._disp_shape is None:
            return None
        gh, gw = self._green_shape
        dh, dw = self._disp_shape
        if gw <= 0 or gh <= 0:
            return None
        return x_green * dw / gw, y_green * dh / gh

    def _green_len_to_disp(self, length: float) -> float | None:
        """Scale a green-plane length (e.g. the aperture radius) to display px."""
        if self._green_shape is None or self._disp_shape is None:
            return None
        gw = self._green_shape[1]
        dw = self._disp_shape[1]
        return length * dw / gw if gw > 0 else None

    def _on_open_fits(self) -> None:
        """Load a saved FITS into the live pipeline (offline replay).

        The frame lands exactly where a camera frame would, so Solve →
        catalog → target selection → Re-run subs all work telescope-less.
        (The Analyze mode keeps its own single-frame inspector.)
        """
        start = str(self._cfg("sessions_path", str(Path.home())) or Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Open FITS", start, "FITS (*.fits *.fit *.fts);;All files (*)"
        )
        if not path:
            return
        try:
            from astropy.io import fits as _fits

            raw = _fits.getdata(path)
            if raw is None or getattr(raw, "ndim", 0) != 2:
                raise ValueError("expected a single 2-D image HDU")
        except Exception as exc:
            self.log_message.emit("ERROR", f"Open FITS: {exc}")
            return
        # The display/measure pipeline speaks the sensor's raw dtype.
        self._engine.ingest_frame(np.ascontiguousarray(raw, dtype=np.uint16))
        self._frame_context_lbl.setText(f"Opened  {Path(path).name}")
        self.log_message.emit(
            "OK", f"Loaded {Path(path).name} — solve to enable catalog & photometry."
        )

    def open_fits(self) -> None:
        """Open a FITS image into Capture from the application File menu."""
        self._on_open_fits()

    def load_fits(self, path: str) -> None:
        """Open a saved FITS for analysis (alias kept for external callers)."""
        self.open_analysis(path)

    def open_analysis(self, path: str) -> None:
        """Open a saved FITS in a floating analysis window.

        The main viewer keeps following the live camera; deep inspection of an
        already-captured sub (stretch, channels, FWHM, region stats) happens in
        a separate, independent window so the two never fight over the display.
        """
        from argos.ui.analysis_window import AnalysisWindow

        win = AnalysisWindow(self._config)
        if not win.load(path):
            self.log_message.emit("ERROR", f"Could not open {Path(path).name}")
            win.deleteLater()
            return
        win.show()
        win.raise_()
        # Keep references so the windows aren't GC'd; prune closed ones.
        self._analysis_windows = [w for w in self._analysis_windows if w.isVisible()]
        self._analysis_windows.append(win)
        self.log_message.emit("OK", f"Analysing {Path(path).name} in a separate window")

    # ------------------------------------------------------------------
    # Plate solving the live frame (§6) — ASTAP with the mount as a hint
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default):
        value = self._config.get(key, default)
        return default if value is None else value

    @pyqtSlot(object, object, str)
    def _on_astrometry_solved(self, _wcs, overlay, summary: str) -> None:
        """A fresh solution arrived from the controller — apply grid + catalog."""
        if (
            self._green_shape is not None
            and self._astrometry.green_shape is not None
            and self._green_shape != self._astrometry.green_shape
        ):
            # Solved against a frame geometry we no longer display (the preview
            # scale / binning changed while the solve was in flight) — its pixel
            # frame doesn't match, so applying it would misplace every overlay.
            self._clear_astrometry()
            self.log_message.emit(
                "INFO", "Solve finished for a different frame geometry — discarded."
            )
            return
        self.log_message.emit("OK", summary)
        self._mount_dock.set_center_available(True)
        self._viewer.set_astrometry_overlay(overlay, self._green_shape)
        self._arm_overlay("grid", True, self._viewer.set_astrometry_enabled)
        self._histogram_dock.set_astrometry_available(True)
        self._histogram_dock.set_astrometry_checked(self._overlay_bar.is_checked("grid"))
        self._remeasure_selection()  # refresh the clicked star's RA/Dec
        self._engine.maybe_fetch_catalog()  # VSX/VSP once per field
        self._schedule_catalog_project()  # re-project cached catalog + targets onto the new WCS
        self._engine.measure_photometry_if_idle()  # sequences measure per saved sub

    # ------------------------------------------------------------------
    # Overlays, catalog + target set (§6 P1)
    # ------------------------------------------------------------------

    def _on_overlay_toggled(self, name: str, on: bool) -> None:
        {
            "grid": self._viewer.set_astrometry_enabled,
            "catalogue": self._viewer.set_field_catalogue_enabled,
            "variables": self._viewer.set_catalog_enabled,
            "galaxies": lambda enabled: self._viewer.set_context_enabled("galaxies", enabled),
            "nebulae_clusters": lambda enabled: self._viewer.set_context_enabled(
                "nebulae_clusters", enabled
            ),
            "exoplanets": lambda enabled: self._viewer.set_context_enabled("exoplanets", enabled),
            "other_objects": lambda enabled: self._viewer.set_context_enabled(
                "other_objects", enabled
            ),
            "comparisons": self._viewer.set_comparison_enabled,
            "targets": self._viewer.set_target_enabled,
            "labels": self._viewer.set_marker_labels_enabled,
        }[name](on)

    def _open_field_catalogue(self) -> None:
        """Open the per-field catalogue controls at their point of use."""
        dialog = AstrometrySettingsDialog(self._config, self, section=SECTION_CATALOG)
        dialog.saved.connect(self._engine.refetch_catalog)
        dialog.saved.connect(self._schedule_catalog_project)
        dialog.saved.connect(self._sync_catalogue_depth)
        dialog.exec()

    def _sync_catalogue_depth(self) -> None:
        maximum = float(self._cfg("catalog.field_stars_mag_limit", 18.0))
        self._overlay_bar.set_magnitude_range(maximum)
        self._display_mag_limit = min(self._display_mag_limit, maximum)
        self._overlay_bar.set_magnitude_limit(self._display_mag_limit)

    @pyqtSlot(float)
    def _on_catalogue_magnitude_changed(self, value: float) -> None:
        """Apply the faint limit immediately from already cached field data."""
        self._display_mag_limit = float(value)
        self._schedule_catalog_project()

    @pyqtSlot(float)
    def _save_catalogue_magnitude(self, value: float) -> None:
        self._config.set("catalog.display_mag_limit", float(value))
        self._config.save()

    def _arm_overlay(self, name: str, has: bool, setter) -> None:
        """Enable a chip when its data exists; auto-show it the first time only."""
        self._overlay_bar.set_available(name, has)
        if has and name not in self._armed:
            self._armed.add(name)
            self._overlay_bar.set_checked(name, True)
            setter(True)

    @pyqtSlot(object)
    def _on_catalog_ready(self, _result) -> None:
        """The engine fetched a fresh VSX/VSP catalog — project it onto the WCS."""
        self._schedule_catalog_project()
        self._refresh_variable_table()

    @pyqtSlot()
    def _schedule_catalog_project(self) -> None:
        """Coalesce expensive overlay redraws without blocking user input."""
        # A very short delay gives Qt a chance to repaint and process a click
        # after a solve/tracker result.  Restarting the timer folds a burst of
        # catalogue, solve and photometry signals into one redraw.
        self._catalog_project_timer.start(75)

    def _overlay_wcs(self):
        """WCS appropriate for catalogue *display*, not aperture measurement.

        The live tracker is rotation+translation in alt-az and deliberately
        translation-only in EQ.  In both cases it keeps an already solved
        field aligned with the latest image while avoiding a false EQ rotation.
        """
        return self._engine.tracked_wcs()

    def _project_catalog(self) -> None:
        """Re-project the engine's cached variables/comparisons/targets onto the WCS.

        In alt-az mode the live tracker follows field rotation/drift between
        solves.  In EQ it follows measured translation only, preserving the
        alignment of markers on a gently drifting live frame.
        """
        variables = self._engine.variables
        field_stars = self._engine.field_stars
        named_objects = self._engine.named_objects
        comparisons = self._engine.comparisons
        wcs, gs = self._overlay_wcs(), self._green_shape
        raw_var_green = project_points(wcs, gs, ((v.ra_deg, v.dec_deg) for v in variables))
        # The VSX table and its overlay share this projection exactly. A local
        # detector is not used as an arbitrary visibility gate: faint, valid
        # catalogue variables still deserve to be shown and can be filtered by
        # their catalogue magnitude in the Variables table.
        self._var_green = raw_var_green
        var_pts = [
            (
                p[0],
                p[1],
                v.is_suspected,
                v.name,
                f"Variable star\n{v.name}\n{self._variable_body(v)}",
            )
            for source_index, (p, v) in enumerate(zip(self._var_green, variables))
            if p and self._variable_is_visible(source_index)
        ]
        self._viewer.set_catalog_markers(var_pts, gs)

        # Cross-match SIMBAD identities onto Gaia before drawing.  One physical
        # source gets one circle and one information card; a second cyan circle
        # at the same coordinate only made the field look duplicated.
        self._named_objects = list(named_objects)
        match_key = (id(field_stars), id(named_objects), len(field_stars), len(named_objects))
        if match_key != self._field_catalogue_match_key:
            self._field_catalogue_match_key = match_key
            (
                self._matched_named_indices,
                self._field_catalogue_names,
            ) = self._match_field_catalogue_names(field_stars, self._named_objects)
        raw_field_catalogue_green = project_points(
            wcs, gs, ((star.ra_deg, star.dec_deg) for star in field_stars)
        )
        detected_only = bool(self._cfg("catalog.field_stars_detected_only", False))
        self._field_catalogue_green = [
            (
                point
                if point is not None
                and self._magnitude_is_visible(star.g_mag)
                and (not detected_only or self._is_detected_near(point))
                else None
            )
            for point, star in zip(raw_field_catalogue_green, field_stars)
        ]
        field_pts = [
            (
                point[0],
                point[1],
                (
                    identity.name
                    if identity is not None
                    and not self._is_generic_catalogue_identifier(identity.name)
                    else ""
                ),
                self._field_catalogue_tooltip(star, identity),
            )
            for point, star, identity in zip(
                self._field_catalogue_green, field_stars, self._field_catalogue_names
            )
            if point
        ]
        raw_named_green = project_points(
            wcs, gs, ((item.ra_deg, item.dec_deg) for item in self._named_objects)
        )
        self._named_objects_green = [
            point if index not in self._matched_named_indices else None
            for index, point in enumerate(raw_named_green)
        ]
        named_by_type: dict[str, list] = {
            "star": [],
            "galaxy": [],
            "nebula_cluster": [],
            "other": [],
        }
        for point, item in zip(self._named_objects_green, self._named_objects):
            if point is None:
                continue
            marker = (point[0], point[1], item.name, self._named_object_tooltip(item))
            category = self._object_category(item.object_type)
            if category == "star":
                named_by_type["star"].append(marker)
            elif category == "galaxy":
                named_by_type["galaxy"].append(marker)
            elif category in {"nebula", "cluster"}:
                named_by_type["nebula_cluster"].append(marker)
            else:
                named_by_type["other"].append(marker)

        # Standalone SIMBAD stars belong to the observer-facing Stars layer.
        # This also leaves useful identities when Gaia is temporarily offline.
        field_pts.extend(named_by_type["star"])
        self._point_identity_green = project_points(
            wcs,
            gs,
            ((item.ra_deg, item.dec_deg) for item in self._point_identities),
        )
        for point, identity in zip(self._point_identity_green, self._point_identities):
            if point is None:
                continue
            marker = (
                point[0],
                point[1],
                identity.name,
                self._point_identity_tooltip(identity),
            )
            category = self._object_category(identity.object_type)
            if category == "star":
                field_pts.append(marker)
            elif category == "galaxy":
                named_by_type["galaxy"].append(marker)
            elif category in {"nebula", "cluster"}:
                named_by_type["nebula_cluster"].append(marker)
            else:
                named_by_type["other"].append(marker)
        field_pts = self._deduplicate_marker_points(field_pts)
        self._viewer.set_field_catalogue_markers(field_pts, gs)

        # The bundled Essential catalogue is a compact offline context layer:
        # galaxies, nebulae and clusters are not Gaia stars and must not be
        # hidden behind a magnitude filter intended for ordinary stars.
        geometry = field_geometry(wcs, gs)
        self._deep_sky_objects = []
        self._exoplanet_hosts = list(self._engine.exoplanet_hosts)
        if geometry is not None:
            ra_deg, dec_deg, radius_deg, _fov_arcmin = geometry
            if bool(self._cfg("catalog.show_essential_objects", True)):
                self._deep_sky_objects = [
                    item
                    for item in essential_catalogue_objects()
                    if separation_arcmin(ra_deg, dec_deg, item.ra_degrees, item.dec_degrees)
                    <= radius_deg * 60.0
                ]
            if not self._exoplanet_hosts and bool(
                self._cfg("catalog.show_cached_exoplanets", True)
            ):
                self._exoplanet_hosts = cached_exoplanet_hosts_in_cone(ra_deg, dec_deg, radius_deg)
        raw_deep_sky_green = project_points(
            wcs,
            gs,
            ((item.ra_degrees, item.dec_degrees) for item in self._deep_sky_objects),
        )
        self._deep_sky_green = [point for point in raw_deep_sky_green]
        essential_by_type: dict[str, list] = {
            "galaxy": [],
            "nebula_cluster": [],
            "other": [],
        }
        for point, item in zip(self._deep_sky_green, self._deep_sky_objects):
            if point is None:
                continue
            marker = (point[0], point[1], item.name, self._deep_sky_tooltip(item))
            category = self._object_category(item.object_type)
            if category == "galaxy":
                essential_by_type["galaxy"].append(marker)
            elif category in {"nebula", "cluster"}:
                essential_by_type["nebula_cluster"].append(marker)
            else:
                essential_by_type["other"].append(marker)

        galaxy_pts = self._deduplicate_marker_points(
            named_by_type["galaxy"] + essential_by_type["galaxy"]
        )
        nebula_cluster_pts = self._deduplicate_marker_points(
            named_by_type["nebula_cluster"] + essential_by_type["nebula_cluster"]
        )
        other_pts = self._deduplicate_marker_points(
            named_by_type["other"] + essential_by_type["other"]
        )
        self._viewer.set_context_markers("galaxies", galaxy_pts, gs)
        self._viewer.set_context_markers("nebulae_clusters", nebula_cluster_pts, gs)
        self._viewer.set_context_markers("other_objects", other_pts, gs)
        raw_exoplanet_green = project_points(
            wcs,
            gs,
            ((host.ra_degrees, host.dec_degrees) for host in self._exoplanet_hosts),
        )
        self._exoplanet_green = list(raw_exoplanet_green)
        exoplanet_pts = [
            (
                point[0],
                point[1],
                host.host_name,
                self._exoplanet_tooltip(host),
            )
            for point, host in zip(self._exoplanet_green, self._exoplanet_hosts)
            if point
        ]
        self._viewer.set_context_markers("exoplanets", exoplanet_pts, gs)
        self._comp_green = project_points(wcs, gs, ((c.ra_deg, c.dec_deg) for c in comparisons))
        # A VSP chart may contain objects below the current frame's detection
        # threshold.  Do not draw a catalogue plus in empty sky: the optional
        # candidate layer is for sources actually seen in this image. Selected
        # stars remain visible separately as the observer's saved selection.
        comp_pts = [
            (
                p[0],
                p[1],
                self._comparison_tooltip(c),
            )
            for p, c in zip(self._comp_green, comparisons)
            if p and self._is_detected_near(p)
        ]
        self._viewer.set_comparison_markers(comp_pts, gs)
        self._arm_overlay("catalogue", bool(field_pts), self._viewer.set_field_catalogue_enabled)
        self._arm_overlay("variables", bool(var_pts), self._viewer.set_catalog_enabled)
        self._arm_overlay(
            "galaxies",
            bool(galaxy_pts),
            lambda enabled: self._viewer.set_context_enabled("galaxies", enabled),
        )
        self._arm_overlay(
            "nebulae_clusters",
            bool(nebula_cluster_pts),
            lambda enabled: self._viewer.set_context_enabled("nebulae_clusters", enabled),
        )
        self._arm_overlay(
            "exoplanets",
            bool(exoplanet_pts),
            lambda enabled: self._viewer.set_context_enabled("exoplanets", enabled),
        )
        self._arm_overlay(
            "other_objects",
            bool(other_pts),
            lambda enabled: self._viewer.set_context_enabled("other_objects", enabled),
        )
        # Once the field is solved every scientific filter remains clickable,
        # even when its current count is zero.  The count explains an empty
        # layer; a disabled grey button looked like a broken feature.
        field_ready = geometry is not None
        for name, count in (
            ("catalogue", len(field_pts)),
            ("variables", len(var_pts)),
            ("galaxies", len(galaxy_pts)),
            ("nebulae_clusters", len(nebula_cluster_pts)),
            ("exoplanets", len(exoplanet_pts)),
            ("other_objects", len(other_pts)),
            ("comparisons", len(comp_pts)),
        ):
            self._overlay_bar.set_available(name, field_ready)
            self._overlay_bar.set_count(name, count)
        # Catalogue candidates are secondary context; unlike the selected
        # ensemble, they must never flood the image by default.
        self._overlay_bar.set_available("comparisons", field_ready)
        if not comp_pts:
            self._viewer.set_comparison_enabled(False)
        self._project_targets()

    def _project_targets(self) -> None:
        tset = self._engine.target_set()
        wcs, gs = self._overlay_wcs(), self._green_shape
        self._target_green = project_points(wcs, gs, ((s.ra_deg, s.dec_deg) for s in tset.stars))
        pts = []
        for p, s in zip(self._target_green, tset.stars):
            if p is None:
                continue
            pts.append((p[0], p[1], s.display_name, s.role, self._target_tooltip(s)))
        self._viewer.set_target_markers(pts, gs)
        self._arm_overlay("targets", bool(pts), self._viewer.set_target_enabled)
        labels_available = bool(
            pts
            or self._var_green
            or self._field_catalogue_green
            or self._named_objects_green
            or self._deep_sky_green
            or self._exoplanet_green
            or self._comp_green
        )
        self._overlay_bar.set_available("labels", labels_available)
        if labels_available and "labels" not in self._armed:
            # Labels are intentionally opt-in. Circles + hover/click identify
            # the field cleanly; enabling every text label by default turns a
            # dense Milky Way image into an unreadable wall of catalogue IDs.
            self._armed.add("labels")
            self._overlay_bar.set_checked("labels", False)
            self._viewer.set_marker_labels_enabled(False)

    def _variable_is_visible(self, source_index: int) -> bool:
        return (
            self._visible_variable_indices is None or source_index in self._visible_variable_indices
        )

    @pyqtSlot(object)
    def _on_variable_visible_rows_changed(self, source_indices) -> None:
        """Keep the VSX overlay identical to the filtered Variables table."""
        self._visible_variable_indices = {int(index) for index in source_indices}
        self._schedule_catalog_project()

    def _magnitude_is_visible(self, magnitude: float | None) -> bool:
        """Apply the observer's Gaia G limit to stellar Gaia markers only."""
        return magnitude is None or float(magnitude) <= self._display_mag_limit

    @staticmethod
    def _match_field_catalogue_names(field_stars, named_objects) -> tuple[set[int], list]:
        """Match Gaia stars to SIMBAD identities within 3 arcsec.

        This used to calculate every separation in nested Python loops on the
        GUI thread.  The same great-circle nearest-neighbour operation is done
        in small NumPy blocks instead, keeping even dense fields responsive.
        """
        names = [None] * len(field_stars)
        if not field_stars or not named_objects:
            return set(), names

        named_ra = np.deg2rad(np.asarray([item.ra_deg for item in named_objects]))
        named_dec = np.deg2rad(np.asarray([item.dec_deg for item in named_objects]))
        named_sin_dec = np.sin(named_dec)
        named_cos_dec = np.cos(named_dec)
        # 3 arcsec, expressed as a cosine to avoid an arccos per candidate.
        min_cosine = math.cos(math.radians(3.0 / 3600.0))
        matched: set[int] = set()

        # Bound temporary arrays when a catalogue request has thousands of
        # Gaia stars, while retaining vectorised work for each block.
        block_size = 256
        for start in range(0, len(field_stars), block_size):
            stars = field_stars[start : start + block_size]
            ra = np.deg2rad(np.asarray([star.ra_deg for star in stars]))
            dec = np.deg2rad(np.asarray([star.dec_deg for star in stars]))
            cosines = np.sin(dec)[:, None] * named_sin_dec[None, :] + np.cos(dec)[
                :, None
            ] * named_cos_dec[None, :] * np.cos(ra[:, None] - named_ra[None, :])
            nearest = np.argmax(cosines, axis=1)
            for offset, index in enumerate(nearest):
                if cosines[offset, index] >= min_cosine:
                    item_index = int(index)
                    matched.add(item_index)
                    names[start + offset] = named_objects[item_index]
        return matched, names

    @staticmethod
    def _deduplicate_marker_points(points, radius_px: float = 8.0) -> list:
        """Merge catalogue overlays that identify the same image position.

        SIMBAD and the bundled Messier/NGC/IC catalogue can describe one
        physical object independently.  Keep one marker while preserving the
        underlying catalogue rows for click identification and provenance.
        """
        selected = []
        radius2 = float(radius_px) ** 2
        for point in points:
            if any(
                (point[0] - existing[0]) ** 2 + (point[1] - existing[1]) ** 2 <= radius2
                for existing in selected
            ):
                continue
            selected.append(point)
        return selected

    @staticmethod
    def _comparison_brightest_mag(comparison) -> float | None:
        values = [band.mag for band in comparison.bands if band.mag is not None]
        return min(values) if values else None

    @staticmethod
    def _host_brightest_mag(host) -> float | None:
        values = [value for _band, value in getattr(host, "mags", ())]
        return min(values) if values else None

    @staticmethod
    def _object_category(object_type: str) -> str:
        """Map catalogue-specific object types to the observer-facing filters."""
        value = (object_type or "").casefold()
        if any(token in value for token in ("gal", "qso", "agn")) or value in {
            "g",
            "gi",
            "gx",
            "gic",
        }:
            return "galaxy"
        if any(token in value for token in ("neb", "hii")) or value in {
            "pn",
            "pl",
            "nb",
            "snr",
        }:
            return "nebula"
        if any(token in value for token in ("cluster", "cl*")) or value in {
            "oc",
            "gc",
            "gb",
            "glc",
            "c+n",
        }:
            return "cluster"
        if "*" in value or "star" in value:
            return "star"
        return "other"

    @staticmethod
    def _is_generic_catalogue_identifier(name: str) -> bool:
        """Identifiers useful in a card but too verbose/redundant as labels."""
        compact = " ".join((name or "").split()).casefold()
        return compact.startswith(
            (
                "gaia ",
                "2mass ",
                "ucac",
                "sdss ",
                "pan-starrs ",
                "ps1 ",
                "wise ",
                "allwise ",
                "lamost ",
                "ztf ",
                "asassn ",
                "ato ",
            )
        )

    @staticmethod
    def _field_catalogue_body(star, identity=None) -> str:
        lines = []
        if identity is not None:
            lines.extend(
                [
                    f"Name  {identity.name}",
                    f"Object type  {identity.object_type or '—'}",
                ]
            )
        lines.append(f"Gaia DR3 source  {star.source_id}")
        for label, value in (
            ("Gaia G", star.g_mag),
            ("Gaia BP", star.bp_mag),
            ("Gaia RP", star.rp_mag),
        ):
            if value is not None:
                lines.append(f"{label} magnitude  {value:.3f}")
        lines.append(f"RA {format_ra_hms(star.ra_deg / 15.0)}  Dec {format_dec_dms(star.dec_deg)}")
        lines.append("Sources  Gaia DR3" + (" · SIMBAD" if identity is not None else ""))
        return "\n".join(lines)

    @classmethod
    def _field_catalogue_tooltip(cls, star, identity=None) -> str:
        return cls._field_catalogue_body(star, identity)

    @staticmethod
    def _deep_sky_tooltip(item) -> str:
        magnitude = f"\nMagnitude  {item.magnitude:.1f}" if item.magnitude is not None else ""
        aliases = f"\nAlso  {', '.join(item.aliases[:2])}" if item.aliases else ""
        return (
            f"Argos Essential Catalogue\n{item.name}\n{item.object_type or 'Deep-sky object'}"
            f"{magnitude}{aliases}\n"
            f"RA {format_ra_hms(item.ra_degrees / 15.0)}  Dec {format_dec_dms(item.dec_degrees)}"
        )

    @staticmethod
    def _named_object_tooltip(item) -> str:
        magnitudes = "".join(
            f"\nSIMBAD {band} magnitude  {value:.3f}" for band, value in getattr(item, "mags", ())
        )
        return (
            f"SIMBAD identified object\n{item.name}\n{item.object_type or 'Object type unavailable'}\n"
            f"{magnitudes.lstrip()}\n"
            f"RA {format_ra_hms(item.ra_deg / 15.0)}  Dec {format_dec_dms(item.dec_deg)}"
        )

    @staticmethod
    def _point_identity_tooltip(identity: PointSourceIdentity) -> str:
        magnitudes = "".join(f"\n{band} magnitude  {value:.3f}" for band, value in identity.mags)
        return (
            f"Point catalogue identification\n{identity.name}\n"
            f"{identity.object_type or 'Object type unavailable'}\n"
            f"Source  {identity.source}{magnitudes}\n"
            f"RA {format_ra_hms(identity.ra_deg / 15.0)}  "
            f"Dec {format_dec_dms(identity.dec_deg)}\n"
            f"WCS match offset  {identity.separation_arcsec:.1f}″"
        )

    @staticmethod
    def _exoplanet_tooltip(host) -> str:
        planets = ", ".join(host.planet_names)
        magnitudes = "".join(
            f"\n{band} magnitude  {value:.3f}" for band, value in getattr(host, "mags", ())
        )
        return (
            f"Prepared exoplanet host\n{host.host_name}\nCached planet(s)  {planets}\n"
            f"{magnitudes.lstrip()}\n"
            f"RA {format_ra_hms(host.ra_degrees / 15.0)}  "
            f"Dec {format_dec_dms(host.dec_degrees)}\n"
            "Source: local NASA Exoplanet Archive cache"
        )

    def _is_detected_near(self, point: tuple[float, float], tolerance_px: float = 4.0) -> bool:
        """Whether a catalogue coordinate has a local detected source nearby."""
        if not self._detected_green:
            return True  # no current detector result: do not manufacture a warning
        x, y = point
        return any(math.hypot(x - dx, y - dy) <= tolerance_px for dx, dy in self._detected_green)

    @staticmethod
    def _comparison_tooltip(comparison) -> str:
        mags = "  ".join(f"{band.band} {band.mag:.3f}" for band in comparison.bands) or "—"
        return (
            "VSP reference candidate\n"
            f"AUID {comparison.auid}\n"
            f"Catalogue magnitudes  {mags}\n"
            f"RA {format_ra_hms(comparison.ra_deg / 15.0)}  "
            f"Dec {format_dec_dms(comparison.dec_deg)}"
        )

    @staticmethod
    def _target_tooltip(star: TargetStar) -> str:
        role = {
            "target": "Photometry target",
            "comparison": "Selected reference star",
            "check": "Check star",
        }.get(star.role, star.role.capitalize())
        mags = "  ".join(f"{band} {mag:.3f}" for band, mag in star.mags.items()) or "—"
        return (
            f"{role}\n{star.display_name}\n"
            f"AUID {star.auid or '—'}\n"
            f"Catalogue magnitudes  {mags}\n"
            f"RA {format_ra_hms(star.ra_deg / 15.0)}  Dec {format_dec_dms(star.dec_deg)}"
        )

    # ------------------------------------------------------------------
    # Target roles (the engine owns the persistent target set)
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def _on_targets_changed(self, _tset) -> None:
        """The engine's target set changed — refresh markers and the tables."""
        self._project_targets()
        self._refresh_target_table()
        self._refresh_variable_table()  # ● markers on the selected rows

    def _on_card_role(self, role: str) -> None:
        if self._pending_star is None:
            return
        star = TargetStar(role=role, **self._pending_star)
        # Before the save, whatever the role: the object name keys targets.json
        # + FITS OBJECT — a first-pick comparison must not orphan the set under
        # the default name.
        self._camera_dock.set_object_name_if_empty(star.display_name)
        self._engine.set_target_role(star)  # → targets_changed refreshes the view
        self.log_message.emit("OK", f"{role.capitalize()}: {star.display_name}")

    def _on_card_remove(self) -> None:
        """Remove the shown star from the target set (image-side pruning)."""
        if self._pending_star is None:
            return
        star = TargetStar(role="target", **self._pending_star)
        self._engine.remove_target(star.key())  # → targets_changed refreshes all
        self.log_message.emit("OK", f"Removed from target set: {star.display_name}")
        self._info_card.hide()
        self._on_card_cleared()

    def _on_card_cleared(self) -> None:
        self._viewer.clear_selection()
        self._selected_green = None
        self._pending_star = None
        self._pending_green = None

    @pyqtSlot(str)
    def _on_photometry_star_selected(self, key: str) -> None:
        """Focus the image on the exact saved-star row selected in Photometry.

        The tables are not a second source of truth: they select a star from the
        engine-owned target set and the image receives the same aperture and
        display label used for a direct click.
        """
        stars = self._engine.target_set().stars
        index = next((i for i, star in enumerate(stars) if star.key() == key), None)
        if index is None or index >= len(self._target_green):
            return
        pos = self._target_green[index]
        if pos is None:
            self.log_message.emit("WARN", "Selected star is outside the solved image.")
            return
        star = stars[index]
        aperture, _inner, _outer = self._engine.photometry_geometry()
        display_pos = self._green_to_disp(pos[0], pos[1])
        if display_pos is None:
            return
        self._overlay_bar.set_checked("targets", True)
        self._viewer.set_target_enabled(True)
        self._viewer.mark_selection(
            display_pos[0],
            display_pos[1],
            f"{star.role.capitalize()} · {star.display_name}",
            self._green_len_to_disp(aperture),
            show_label=True,
        )

    # ------------------------------------------------------------------
    # Live photometry preview (§6 P4)
    # ------------------------------------------------------------------

    def open_photometry(self, *, select_variable: bool = False) -> None:
        """Shell entry point for field-star selection or live diagnostics."""
        self._open_photometry(select_variable=select_variable)

    def rerun_subs(self) -> None:
        """Shell menu entry point (Photometry ▸ Re-run subs…)."""
        self._on_rerun_subs()

    def _open_photometry(self, *, select_variable: bool = False) -> None:
        if self._photometry_window is None:
            self._photometry_window = PhotometryWindow(self)
            self._photometry_window.lightcurves = self._engine.lightcurves
            self._photometry_window.set_export_meta(
                str(self._cfg("observer.obscode", "XXX") or "XXX"),
                str(self._cfg("photometry.default_band", "TG") or "TG"),
                self._engine.target_set().object_name,
            )
            self._photometry_window.targets.remove_requested.connect(self._engine.remove_target)
            self._photometry_window.comparisons.remove_requested.connect(self._engine.remove_target)
            self._photometry_window.targets.star_selected.connect(self._on_photometry_star_selected)
            self._photometry_window.comparisons.star_selected.connect(
                self._on_photometry_star_selected
            )
            self._photometry_window.variables.target_requested.connect(
                self._on_variable_target_requested
            )
            self._photometry_window.variables.visible_rows_changed.connect(
                self._on_variable_visible_rows_changed
            )
            self._photometry_window.comparisons.refresh_requested.connect(
                self._engine.refetch_catalog
            )
            self._photometry_window.comparisons.recommend_requested.connect(
                self._recommend_comparison_stars
            )
            self._photometry_window.comparisons.auto_count_changed.connect(
                self._on_auto_comparison_count_changed
            )
            self._photometry_window.setup.setting_changed.connect(
                self._on_photometry_setting_changed
            )
        # Backfill the window with the curve so far (points accrue in the dock).
        self._photometry_window.load_curves(
            self._engine.lightcurves,
            obscode=str(self._cfg("observer.obscode", "XXX") or "XXX"),
            filt=str(self._cfg("photometry.default_band", "TG") or "TG"),
        )
        # ``load_curves`` snapshots into a copy; restore the engine's live dict so
        # AAVSO/CSV export reflects points (and targets) added after this open.
        self._photometry_window.lightcurves = self._engine.lightcurves
        self._refresh_target_table()
        self._photometry_window.comparisons.set_quality_report(
            self._engine.comparison_quality_report
        )
        self._photometry_window.comparisons.set_auto_count(
            int(self._cfg("photometry.auto_comparisons", 5))
        )
        self._refresh_variable_table()
        self._sync_photometry_setup()
        if select_variable:
            self._photometry_window.show_variable_stars()
        self._photometry_window.show()
        self._photometry_window.raise_()

    def _sync_photometry_setup(self) -> None:
        """Keep the visible measurement geometry tied to the active engine."""
        if self._photometry_window is not None:
            self._photometry_window.setup.set_values(self._cfg, self._engine.photometry_geometry())

    @pyqtSlot(str, object)
    def _on_photometry_setting_changed(self, key: str, value: object) -> None:
        self._config.set(key, value)
        self._sync_photometry_setup()
        # If a catalogue star is selected, redraw its aperture ring immediately
        # so the control gives direct, unambiguous visual feedback.
        if self._pending_green is not None:
            dp = self._green_to_disp(*self._pending_green)
            if dp is not None:
                aperture, _annulus_in, _annulus_out = self._engine.photometry_geometry()
                self._viewer.mark_selection(
                    dp[0], dp[1], "", self._green_len_to_disp(aperture), show_label=False
                )
        self.log_message.emit("INFO", "Photometry measurement geometry updated.")

    @pyqtSlot(int)
    def _on_auto_comparison_count_changed(self, count: int) -> None:
        self._config.set("photometry.auto_comparisons", int(count))
        added = self._engine.reselect_comparison_stars()
        if added:
            self.log_message.emit(
                "OK", f"Photometry: selected {added} pilot-qualified comparison(s)."
            )
        else:
            selected = len(self._engine.target_set().by_role("comparison"))
            self.log_message.emit(
                "INFO",
                (
                    f"Photometry: requested {count} comparison(s); "
                    f"{selected} pilot-qualified VSP star(s) selected."
                ),
            )

    def _recommend_comparison_stars(self) -> None:
        """Explicitly replace VSP suggestions with pilot-frame quality picks."""
        added = self._engine.reselect_comparison_stars()
        if added:
            self.log_message.emit(
                "OK", f"Photometry: replaced the VSP ensemble with {added} pilot-qualified star(s)."
            )
        else:
            self.log_message.emit(
                "WARN",
                "Photometry: no pilot-qualified comparison found — solve a full-resolution frame first.",
            )

    def _refresh_target_table(self) -> None:
        if self._photometry_window is not None:
            self._photometry_window.set_targets(self._engine.target_set().stars)

    def _refresh_variable_table(self) -> None:
        """Feed the Variables tab: one row per cached VSX variable, in order."""
        if self._photometry_window is None:
            return
        centre = field_geometry(self._astrometry.wcs, self._green_shape)
        saved = {s.key() for s in self._engine.target_set().stars}
        rows = []
        source_indices = []
        for source_index, (v, point) in enumerate(zip(self._engine.variables, self._var_green)):
            if point is None:
                continue  # outside the solved rectangular image
            sep = (
                separation_arcmin(centre[0], centre[1], v.ra_deg, v.dec_deg)
                if centre is not None
                else None
            )
            mag = " – ".join(m for m in (v.max_mag, v.min_mag) if m)
            key = f"auid:{v.auid}" if v.auid else f"pos:{v.ra_deg:.5f},{v.dec_deg:.5f}"
            rows.append((v.name, v.var_type, mag, v.period, sep, key in saved, v.is_suspected))
            source_indices.append(source_index)
        self._photometry_window.variables.set_variables(rows, source_indices=source_indices)

    def _on_variable_target_requested(self, row: int) -> None:
        """Variables-tab pick — the same action as the star-card Target button."""
        variables = self._engine.variables
        if not 0 <= row < len(variables):
            return
        v = variables[row]
        star = TargetStar(
            role="target",
            ra_deg=v.ra_deg,
            dec_deg=v.dec_deg,
            auid=v.auid,
            name=v.name,
            source="vsx",
            mags={},
        )
        # Before the save: the object name keys targets.json + FITS OBJECT.
        self._camera_dock.set_object_name_if_empty(star.display_name)
        self._engine.set_target_role(star)  # → targets_changed refreshes the view
        self.log_message.emit("OK", f"Target: {star.display_name}")

    # ------------------------------------------------------------------
    # Batch re-run over saved subs (WS7 — off the UI thread)
    # ------------------------------------------------------------------

    def _on_rerun_subs(self) -> None:
        """Re-run differential photometry over a folder of saved FITS.

        Uses the current live solve as the shared WCS and the engine's target
        set — the same measurement core as the live path — so comps carry their
        catalog magnitudes. Runs in a QThread with a cancellable progress dialog.
        """
        if self._batch_worker is not None:
            return  # one at a time
        from argos.core.photometry.params import PhotometryParams
        from argos.workers.photometry_batch_worker import BatchRequest, PhotometryBatchWorker

        wcs = self._astrometry.wcs
        if wcs is None:
            QMessageBox.information(
                self, "Solve first", "Plate-solve the live frame before a batch re-run."
            )
            return
        tset = self._engine.target_set()
        if not tset.by_role("target"):
            QMessageBox.information(self, "No targets", "Assign at least one target star first.")
            return
        if not tset.by_role("comparison"):
            # Without comps every frame ends "no valid comparisons" → an empty
            # curve; refuse up front with the fix instead of a post-run WARN.
            QMessageBox.information(
                self,
                "No comparison stars",
                "The target set has no comparison stars, so differential\n"
                "photometry cannot produce magnitudes.\n\n"
                "Solve the field and wait for the catalog (comps auto-select\n"
                "once it arrives), or click a comparison marker on the image\n"
                "and assign it the Comp role.",
            )
            return
        start = str(self._engine.last_sequence_dir or self._config.sessions_path)
        folder = QFileDialog.getExistingDirectory(self, "Folder of saved subs", start)
        if not folder:
            return
        paths = sorted(Path(folder).glob("*.fits"), key=lambda p: p.stat().st_mtime)
        if not paths:
            paths = sorted(Path(folder).rglob("*.fits"), key=lambda p: p.stat().st_mtime)
        if not paths:
            QMessageBox.information(self, "No frames", f"No FITS files in {folder}")
            return
        params = PhotometryParams.from_config(self._cfg, egain=self._engine_egain())
        req = BatchRequest(
            fits_paths=paths,
            wcs=wcs,
            target_set=tset,
            params=params,
            out_dir=self._config.sessions_path.parent / "targets",
            object_name=tset.object_name or "untitled",
            site=(
                self._cfg("site.latitude", None),
                self._cfg("site.longitude", None),
                self._cfg("site.elevation", 0.0) or 0.0,
            ),
            diagnostics=bool(self._cfg("diagnostics.enabled", False)),
        )
        dialog = QProgressDialog("Re-running photometry…", "Cancel", 0, len(paths), self)
        dialog.setWindowTitle("Batch photometry")
        dialog.setMinimumDuration(0)
        worker = PhotometryBatchWorker(req, parent=self)
        dialog.canceled.connect(worker.cancel)
        worker.progress.connect(lambda done, total: dialog.setValue(done))
        worker.finished_batch.connect(self._on_batch_done)
        self._batch_worker = worker
        self._batch_dialog = dialog
        dialog.show()
        worker.start()

    def _engine_egain(self) -> float:
        """e-/ADU for an offline batch: config table, then the sensor reference.

        Mirrors ``AcquisitionEngine._egain`` minus the driver step, which needs
        a live handle. Falling back to 1.0 here — as this used to — silently
        treated ADU as electrons and made every batch SNR and formal error
        wrong, without any warning.
        """
        gain = self._camera_dock.params().gain
        table = self._cfg("camera.egain_table", {}) or {}
        if isinstance(table, dict) and str(gain) in table:
            try:
                return float(table[str(gain)])
            except (TypeError, ValueError):
                pass
        return sensor_models.lookup_egain(hardware.profile().sensor, gain)

    @pyqtSlot(object)
    def _on_batch_done(self, result) -> None:
        if self._batch_dialog is not None:
            self._batch_dialog.reset()
            self._batch_dialog = None
        self._batch_worker = None
        if not result.ok:
            QMessageBox.warning(self, "Batch failed", result.error)
            return
        if not result.curves:
            self.log_message.emit(
                "WARN",
                "Batch photometry: no points measured — see the per-frame notes "
                "in targets/*_diagnostics.jsonl.",
            )
            return
        self.log_message.emit(
            "OK",
            f"Batch photometry: {result.frames_done} frame(s), {len(result.curves)} target(s).",
        )
        if abs(result.rotation_deg) > 0.05 or result.shift_px > 1.0:
            self.log_message.emit(
                "INFO",
                f"Apertures tracked the field: {result.rotation_deg:+.2f}° rotation, "
                f"{result.shift_px:.1f} px drift vs the reference solve.",
            )
        # Batch curves land in THE store; curves_changed re-renders the dock
        # and (via the open below) the window from the same dict — the old
        # window-only feed left the Light-curve dock empty after a re-run.
        self._engine.adopt_curves(result.curves)
        self._open_photometry()

    @pyqtSlot()
    def _on_photometry_measuring(self) -> None:
        """A measurement pass starts — sample the CCD temp for the metrics panel."""
        win = self._photometry_window
        if win is not None and win.isVisible():
            win.metrics.add_sample(self._elapsed(), temp=self._ccd_temp())

    def _render_curves(self) -> None:
        """Re-render every curve surface from the engine's store (the ONLY
        rendering path for whole-store changes: batch adopt, object change)."""
        self._lightcurve_panel.set_curves(self._engine.lightcurves)
        win = self._photometry_window
        if win is not None:
            win.load_curves(
                self._engine.lightcurves,
                obscode=str(self._cfg("observer.obscode", "XXX") or "XXX"),
                filt=str(self._cfg("photometry.default_band", "TG") or "TG"),
            )
            # load_curves snapshots into a copy; keep exports on the live store.
            win.lightcurves = self._engine.lightcurves

    @pyqtSlot(object)
    def _on_photometry_point(self, point) -> None:
        """Render one differential point (typed PhotometryPoint) on the curve.

        The live dock always gets the point (it may be hidden but stays in sync);
        the floating window mirrors it only while shown."""
        self._lightcurve_panel.add_point(
            point.name,
            point.jd,
            point.mag,
            point.mag_err,
            saturated=point.saturated,
            role=point.role,
        )
        win = self._photometry_window
        if win is not None and win.isVisible():
            win.feed_point(point)

    @pyqtSlot(object)
    def _on_comparison_quality_updated(self, report) -> None:
        """Mirror the persisted live vetting result into Photometry immediately."""
        if self._photometry_window is not None:
            self._photometry_window.comparisons.set_quality_report(report)

    def _elapsed(self) -> float:
        if self._metrics_t0 is None:
            self._metrics_t0 = time.monotonic()
        return time.monotonic() - self._metrics_t0

    def _feed_metrics(self, pf) -> None:
        win = self._photometry_window
        if win is None or not win.isVisible():
            return
        m = pf.metrics
        pos = self._session.last_position
        air = airmass_from_altitude(pos.altitude) if pos else None
        win.metrics.add_sample(
            self._elapsed(),
            sky=m.sky_adu,
            fwhm=pf.stars.mean_fwhm,
            hfd=m.hfd,
            stars=m.star_count,
            airmass=air,
        )

    def _ccd_temp(self) -> float | None:
        """Sensor temperature (read only when the photometry window is open — a
        solved frame is infrequent, so this off-cadence network read is cheap)."""
        return self._session.ccd_temperature()

    def _clear_astrometry(self) -> None:
        """Drop the WCS + catalog overlays — a slew/goto changes the field."""
        self._engine.invalidate_astrometry()  # WCS + the engine's catalog cache
        self._mount_dock.set_center_available(False)
        self._viewer.set_astrometry_overlay(None)
        self._histogram_dock.set_astrometry_available(False)
        self._histogram_dock.set_astrometry_checked(False)
        # The field changed → drop the projections (re-fetched on the next
        # solve) and re-arm so the overlays auto-show again for the new field.
        self._var_green = []
        self._visible_variable_indices = None
        self._field_catalogue_green = []
        self._field_catalogue_names = []
        self._field_catalogue_match_key = None
        self._matched_named_indices = set()
        self._point_identities = []
        self._point_identity_green = []
        self._point_identity_request = None
        self._named_objects = []
        self._named_objects_green = []
        self._deep_sky_objects = []
        self._deep_sky_green = []
        self._exoplanet_hosts = []
        self._exoplanet_green = []
        self._comp_green = []
        self._target_green = []
        self._detected_green = []
        self._armed.clear()
        self._viewer.set_catalog_markers((), self._green_shape)
        self._viewer.set_field_catalogue_markers((), self._green_shape)
        self._viewer.set_context_markers("galaxies", (), self._green_shape)
        self._viewer.set_context_markers("nebulae_clusters", (), self._green_shape)
        self._viewer.set_context_markers("exoplanets", (), self._green_shape)
        self._viewer.set_context_markers("other_objects", (), self._green_shape)
        self._viewer.set_comparison_markers((), self._green_shape)
        self._viewer.set_target_markers((), self._green_shape)
        for name in (
            "grid",
            "catalogue",
            "variables",
            "galaxies",
            "nebulae_clusters",
            "exoplanets",
            "other_objects",
            "comparisons",
            "targets",
            "labels",
        ):
            self._overlay_bar.set_available(name, False)
            if name in {
                "catalogue",
                "variables",
                "galaxies",
                "nebulae_clusters",
                "exoplanets",
                "other_objects",
                "comparisons",
            }:
                self._overlay_bar.set_count(name, 0)

    # ------------------------------------------------------------------
    # Driver-backed camera parameters (offset / binning)
    # ------------------------------------------------------------------

    def _on_camera_offset(self, value: int) -> None:
        cam = self._session.camera
        if not cam:
            return
        owner = self._engine.camera_owner()
        if owner is not None:
            # Offset is device-global — changing it mid-run would shift the
            # bias level of the frames the worker is acquiring.
            self.log_message.emit("WARN", f"{owner} running — it owns the camera. Stop it first.")
            offset = cam.get_offset()  # re-sync the spinbox with the device
            if offset is not None and cam.offset_min is not None and cam.offset_max is not None:
                self._camera_dock.set_offset_support(cam.offset_min, cam.offset_max, offset)
            return
        self._session.set_camera_offset(value)

    def _on_camera_binning(self, value: int) -> None:
        cam = self._session.camera
        if not cam:
            return
        owner = self._engine.camera_owner()
        if owner is not None:
            # Binning is device-global — changing it mid-run would change the
            # frame geometry (and plate scale) under the worker.
            self.log_message.emit("WARN", f"{owner} running — it owns the camera. Stop it first.")
            self._camera_dock.set_binning_support(cam.max_bin, cam.get_binning())
            return
        self._session.set_camera_binning(value)

    # ------------------------------------------------------------------
    # Sequence — the run is driven from the Sequencer page; Capture only
    # reacts to it (auto-solve arming, per-frame log lines)
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    def _on_sequence_running(self, running: bool) -> None:
        # Arm the periodic re-solve for the run (and restore the user's choice
        # after): it re-bases the WCS + live tracker so rigid-fit error never
        # accumulates, keeping markers and apertures on the stars all night.
        if running:
            self._auto_solve_before_seq = self._astrometry.auto
            if self._astrometry.wcs is not None and not self._astrometry.auto:
                self.auto_solve_armed.emit(True)  # menu action → set_auto via toggled
                self.log_message.emit("INFO", "Sequence: auto-solve armed for the run.")
        elif not getattr(self, "_auto_solve_before_seq", True):
            self.auto_solve_armed.emit(False)
        if not running:
            self._sequence_context_lbl.clear()

    @pyqtSlot(str, int, int, float)
    def _on_sequence_progress(self, _object_name: str, done: int, total: int, eta_s: float) -> None:
        """Keep the image context bar focused on the only live count users need."""
        if total <= 0:
            self._sequence_context_lbl.clear()
            return
        minutes, seconds = divmod(max(0, int(eta_s)), 60)
        self._sequence_context_lbl.setText(
            f"Sequence  {done}/{total}  ·  ETA {minutes}m {seconds:02d}s"
        )

    def _on_seq_frame_saved(self, path: str, record) -> None:
        name = Path(path).name
        self._frame_context_lbl.setText(f"Saved  {name}")
        if record is not None and record.hfd is not None:
            fwhm = f" FWHM={record.fwhm:.1f}" if record.fwhm is not None else ""
            self.log_message.emit(
                "OK", f"Saved {name}  HFD={record.hfd:.1f}{fwhm}  stars={record.star_count}"
            )
        else:
            self.log_message.emit("OK", f"Saved {name}")

    # ------------------------------------------------------------------
    # Frames — every engine frame (live, single, sequence) lands here
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def _on_frame(self, frame: LiveFrame) -> None:
        """Render an engine frame (the engine already handled saves/settings)."""
        self._show_raw(frame.preview)

    # ------------------------------------------------------------------
    # Mount actions
    # ------------------------------------------------------------------

    def _on_jog_stop(self, axis: int) -> None:
        self._session.jog(axis, 0.0)

    @pyqtSlot(object)
    def _on_mount_position(self, pos: MountPosition) -> None:
        self._mount_dock.set_position(
            pos.ra,
            pos.dec,
            pos.altitude,
            pos.azimuth,
            pos.tracking,
            pos.slewing,
        )
        # A slew initiated outside Argos (e.g. the Seestar app or Stellarium)
        # does not emit DeviceSession.slewed.  Never retain the previous
        # solution in that case: after a failed re-solve it would project a
        # scientifically invalid target ring onto the new image.
        if self._astrometry.mount_moved_since_solution((pos.ra, pos.dec)):
            self._clear_astrometry()
            self.log_message.emit(
                "INFO",
                "Mount pointing changed outside Argos — stale astrometry overlays cleared. "
                "Plate-solve the current full-resolution frame.",
            )

    def _on_goto(self, ra_h: float, dec_d: float) -> None:
        # The session emits ``slewed`` on success → _clear_astrometry.
        self._session.goto(ra_h, dec_d)

    def _center_selected_target(self) -> None:
        """Use the visible solved frame for one explicit centering correction."""
        wcs, shape = self._astrometry.wcs, self._green_shape
        if wcs is None or shape is None:
            self.log_message.emit("WARN", "Center target: plate-solve the current frame first.")
            return
        targets = self._engine.target_set().by_role("target")
        if targets:
            target = targets[0]
            target_ra_h, target_dec_d = target.ra_deg / 15.0, target.dec_deg
            label = target.display_name
        elif self._session.target_radec is not None:
            target_ra_h, target_dec_d = self._session.target_radec
            label = "current GoTo target"
        else:
            self.log_message.emit("WARN", "Center target: select a target or issue a GoTo first.")
            return
        height, width = shape
        solved_ra_h, solved_dec_d = wcs.pixel_to_radec((width - 1) / 2, (height - 1) / 2)
        self._mount_dock.set_goto_fields(target_ra_h, target_dec_d)
        self.log_message.emit("INFO", f"Centering {label} from the current plate solution…")
        self._session.center_target_from_solution(
            solved_ra_h, solved_dec_d, target_ra_h, target_dec_d
        )

    def _lookup_object(self, query: str) -> None:
        """Resolve a designation in a disposable worker; never freeze Capture."""
        if self._object_resolver_worker is not None:
            return
        self._mount_dock.set_lookup_busy(True)
        worker = ObjectResolverWorker(query, self)
        self._object_resolver_worker = worker
        worker.resolved.connect(self._on_object_resolved)
        worker.failed.connect(self._on_object_lookup_failed)
        worker.finished.connect(self._finish_object_lookup)
        worker.start()

    @pyqtSlot(object)
    def _on_object_resolved(self, result) -> None:
        self.set_catalogue_target(result)

    @pyqtSlot(object)
    def set_catalogue_target(self, result) -> None:
        """Apply a catalogue choice made here or in the Sequencer workspace."""
        self._mount_dock.set_resolved_object(result)
        # Object is a shared observation identity.  Updating it here keeps
        # Capture paths, FITS OBJECT and the Sequence plan in agreement before
        # the observer elects to slew.
        self._camera_dock.set_object_name(result.name, emit=True)
        self.target_coordinates_changed.emit(result.name, result.ra_hours, result.dec_degrees)
        self.log_message.emit(
            "INFO",
            f"Catalogue target: {result.name} — RA {result.ra_hours:.5f}h Dec {result.dec_degrees:+.5f}°",
        )
        # A regular catalogue lookup is deliberately only an observation
        # identity: pointing at M 42 must not silently start a photometry
        # target set.  SIMBAD's ``V*`` type is different: the observer has
        # selected a variable star such as XX Cyg, for which Target + automatic
        # VSP comparison selection is the expected scientific workflow.
        if self._is_variable_star(result):
            self._set_photometry_target("variable_catalogue", result, announce=False)

    @staticmethod
    def _is_variable_star(result) -> bool:
        """Whether a resolver result is explicitly classified as variable."""
        return is_variable_object_type(str(getattr(result, "object_type", "") or ""))

    def _set_photometry_target(self, source: str, result, *, announce: bool = True) -> None:
        """Save one resolved source as the active differential target."""
        star = TargetStar(
            role="target",
            ra_deg=result.ra_degrees,
            dec_deg=result.dec_degrees,
            name=result.name,
            source=source,
            mags={},
        )
        self._engine.set_target_role(star)
        if self._astrometry.wcs is None:
            self.log_message.emit(
                "INFO",
                "Photometry target saved — solve a full-resolution frame to draw its marker "
                "and select field comparison stars.",
            )
        if announce:
            label = "Exoplanet host" if source == "exoplanet_host" else "Variable star"
            self.log_message.emit(
                "OK",
                f"{label} selected for photometry: {star.display_name}. "
                "Identify the field, then review the comparison ensemble.",
            )

    @pyqtSlot(str, object)
    def set_science_source(self, programme: str, result) -> None:
        """Turn a planned source into the shared photometry target.

        The Sequencer has already updated the observation identity before this
        signal is emitted.  Creating the target now means variable-star and
        exoplanet observations enter the identical target/comparison workflow;
        the solved-field catalogue later supplies or lets the observer review
        comparison stars.
        """
        source = "exoplanet_host" if programme == "exoplanet" else "variable_catalogue"
        self._set_photometry_target(source, result)

    @pyqtSlot(str)
    def _on_object_lookup_failed(self, message: str) -> None:
        self._mount_dock.set_lookup_error(message)
        self.log_message.emit("WARN", f"Catalogue lookup: {message}")

    def _finish_object_lookup(self) -> None:
        self._mount_dock.set_lookup_busy(False)
        worker = self._object_resolver_worker
        self._object_resolver_worker = None
        if worker is not None:
            worker.deleteLater()

    def suggest_target_from_coordinates(
        self, ra_h: float, dec_d: float, *, allow_network: bool = False
    ) -> None:
        """Offer, but never impose, a name for a Stellarium coordinate GoTo."""
        if self._nearby_object_resolver_worker is not None:
            return
        self._mount_dock.set_target_suggestion_busy(True)
        worker = NearbyObjectResolverWorker(ra_h, dec_d, allow_network, self)
        self._nearby_object_resolver_worker = worker
        worker.resolved.connect(self._on_nearby_objects_resolved)
        worker.failed.connect(self._on_nearby_object_lookup_failed)
        worker.finished.connect(self._finish_nearby_object_lookup)
        worker.start()

    @pyqtSlot(object)
    def _on_nearby_objects_resolved(self, candidates) -> None:
        self._mount_dock.set_target_suggestions(candidates)
        if candidates:
            self.log_message.emit(
                "INFO",
                f"Stellarium target-name suggestion: {len(candidates)} nearby catalogue candidate(s).",
            )

    @pyqtSlot(str)
    def _on_nearby_object_lookup_failed(self, message: str) -> None:
        self._mount_dock.set_target_suggestions([])
        self.log_message.emit("WARN", f"Target-name suggestion: {message}")

    def _finish_nearby_object_lookup(self) -> None:
        worker = self._nearby_object_resolver_worker
        self._nearby_object_resolver_worker = None
        if worker is not None:
            worker.deleteLater()

    def _goto_resolved_object(self, ra_h: float, dec_d: float, label: str) -> None:
        """The second, intentional action after a successful catalogue search."""
        if not self._session.telescope:
            self.log_message.emit("WARN", "Slew requested but mount not connected")
            return
        self.log_message.emit("CMD", f"Catalogue slew {label} → RA {ra_h:.4f}h Dec {dec_d:+.4f}°")
        self._on_goto(ra_h, dec_d)

    def goto_target(self, ra_h: float, dec_d: float, label: str = "") -> None:
        """Slew to ``(ra, dec)`` from an external source (Stellarium, wizard).

        Pre-fills the mount dock's goto fields so the user can see where the
        request came from, then triggers the same slew code path that the UI
        button uses.
        """
        if not self._session.telescope:
            self.log_message.emit("WARN", "Goto requested but mount not connected")
            return
        self._mount_dock.set_goto_fields(ra_h, dec_d)
        prefix = f"Stellarium {label}" if label else "Goto"
        self.log_message.emit("CMD", f"{prefix} → RA {ra_h:.4f}h Dec {dec_d:+.4f}°")
        self._on_goto(ra_h, dec_d)

    def _open_jog(self) -> None:
        telescope = self._session.telescope
        if not telescope:
            return
        if self._jog_dialog is None:
            self._jog_dialog = ManualControlDialog(telescope, parent=self)
            self._jog_dialog.log_message.connect(self.log_message)
        self._jog_dialog.show()
        self._jog_dialog.raise_()

    # ------------------------------------------------------------------
    # Focuser actions
    # ------------------------------------------------------------------

    def _on_focuser_step(self, delta: int) -> None:
        target = self._session.focuser_step(delta)
        if target is not None:
            self._focuser_dock.set_position(target)

    def _on_focuser_move_to(self, position: int) -> None:
        if self._session.focuser_move_to(position):
            self._focuser_dock.set_position(position)

    def _on_focuser_halt(self) -> None:
        if not self._session.focuser:
            return
        self._engine.stop_autofocus()
        self._session.focuser_halt()

    def request_autofocus(self) -> None:
        """Public entry point (e.g. the Focus screen) to start/stop a sweep."""
        self._engine.request_autofocus()

    def nudge_focuser(self, delta: int) -> None:
        """Public manual focuser nudge (signed steps; + = inward)."""
        self._on_focuser_step(delta)

    @pyqtSlot(bool)
    def _on_autofocus_state(self, running: bool) -> None:
        self._focuser_dock.set_autofocus_running(running)
        self.autofocus_state.emit(running)  # → the Focus screen + sidebar dot

    @pyqtSlot(int, int, int, object)
    def _on_af_step(self, step: int, total: int, pos: int, hfd) -> None:
        self._focuser_dock.set_position(pos)
        hfd_str = f"{hfd:.1f}" if hfd is not None else "—"
        self._focuser_dock.set_autofocus_status(f"Step {step}/{total}  HFD={hfd_str}")
        self._focuser_dock.vcurve.add_sample(pos, hfd)
        self.autofocus_step.emit(step, total, pos, hfd)
        self.log_message.emit("INFO", f"AF {step}/{total}  pos={pos}  HFD={hfd_str}")

    @pyqtSlot(int, object)
    def _on_af_done(self, best_pos: int, best_hfd) -> None:
        self._focuser_dock.set_position(best_pos)
        hfd_str = f"{best_hfd:.1f}" if best_hfd is not None else "—"
        self._focuser_dock.vcurve.set_best(best_pos, best_hfd)
        self.autofocus_best.emit(best_pos, best_hfd)
        self.log_message.emit("OK", f"Autofocus complete — best pos={best_pos}  HFD={hfd_str}")
        self.action_changed.emit(f"Focused  pos={best_pos}")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Close the view's windows, then stop the engine's workers.

        The Shell calls this before ``session.shutdown()`` — the engine's
        workers hold device references, so they must stop first.
        """
        for win in self._analysis_windows:
            win.close()
        self._analysis_windows.clear()
        if self._photometry_window is not None:
            self._photometry_window.close()
            self._photometry_window = None
        if self._batch_worker is not None:
            self._batch_worker.cancel()
            self._batch_worker.wait(3000)
            self._batch_worker = None
        if self._object_resolver_worker is not None:
            self._object_resolver_worker.wait(9000)  # resolver request timeout is 8 seconds
            self._object_resolver_worker = None
        if self._point_identity_worker is not None:
            # Gaia and SIMBAD have independent bounded requests (20 s + 15 s).
            self._point_identity_worker.wait(38000)
            self._point_identity_worker = None
        self._engine.shutdown()
        self._processor.stop()
        self._processor.wait(2000)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        if self._info_card.isVisible():
            self._info_card.reposition()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
