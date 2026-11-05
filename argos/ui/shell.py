"""Shell — the Argos main window. NINA-style: the night happens in ONE screen.

The main workflow is:

* **Equipment** — connect the Seestar devices and Stellarium server.
* **Capture** — live image, camera, mount, focus and photometry workspace.
* **Plan** — target lookup, altitude preview and observing sequences.
* **Analyze** — reload, vet and export saved photometry.
* **Settings** — observer, site, data paths and appearance.

The session layer owns the hardware (WS5): ``DeviceSession`` holds the device
handles, pollers and the Stellarium server; ``AcquisitionEngine`` holds the
camera-ownership state machine and the capture/solve/photometry workers. The
Capture page (``ImagingPage``) is a view over the two. The Equipment page
emits connect/disconnect intents that the Shell routes to the DeviceSession;
device-state updates flow back to the status bar and the Equipment page.
Targeting is driven by Stellarium (select an object, Ctrl+1) over the TCP
telescope-control protocol, or by the mount dock's goto fields.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt, QUrl
from PyQt6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from argos import __version__
from argos.core.config import Config
from argos.core.session.acquisition_engine import AcquisitionEngine
from argos.core.session.device_session import DeviceSession
from argos.core.support_bundle import create_support_bundle, diagnostics_directory
from argos.ui import theme
from argos.ui.pages.configuration_page import ConfigurationPage
from argos.ui.pages.connection_page import ConnectionPage
from argos.ui.pages.imaging_page import ImagingPage
from argos.ui.pages.analyze_page import AnalyzeScreen
from argos.ui.pages.sequencer_page import SequencerPage
from argos.ui.sidebar import MODES, Sidebar
from argos.ui.statusbar import TopStatusBar
from argos.ui.widgets.astrometry_settings import (
    SECTION_ASTROMETRY,
    SECTION_CATALOG,
    AstrometrySettingsDialog,
)
from argos.workers.camera_service import CameraState
from argos.workers.network_monitor import NetworkMonitor

logger = logging.getLogger(__name__)

_CFG_GEOMETRY = "ui.shell.geometry"
_CFG_STATE = "ui.shell.state"
_CFG_MODE = "ui.shell.mode"
_PROJECT_URL = "https://github.com/jperret21/argos"

#: Modes that need hardware to be useful. Devices are never connected at
#: startup, so restoring one of these would land the user on a dead screen —
#: we land on Equipment instead (the persisted mode is kept for the session).
_HARDWARE_MODES = frozenset({"capture"})


class Shell(QMainWindow):
    """Three-mode workspace shell."""

    APP_VERSION = __version__

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

        self.setWindowTitle(f"Argos  v{self.APP_VERSION}")
        self.setMinimumSize(1100, 700)
        self.resize(1440, 900)

        self._build_layout()
        self._build_menu()
        self._wire_signals()
        self._restore_state()

        last_mode = self._config.get(_CFG_MODE) or "connect"
        if last_mode not in self._pages or last_mode in _HARDWARE_MODES:
            last_mode = "connect"
        self._sidebar.select(last_mode)
        self._refresh_sidebar_states()

        logger.info("Shell initialised (mode=%s)", last_mode)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        central = QWidget()
        central.setStyleSheet(f"background:{theme.BG};")
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self._status = TopStatusBar()
        v.addWidget(self._status)

        self._stack = QStackedWidget()
        v.addWidget(self._stack, 1)

        self.setCentralWidget(central)

        self._sidebar = Sidebar(self)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, self._sidebar)

        # The session layer: DeviceSession owns the device handles/pollers
        # and the Stellarium server; AcquisitionEngine owns the camera
        # ownership state machine + capture/solve/photometry workers. The
        # pages are views over the two.
        self._session = DeviceSession(self._config, parent=self)
        self._engine = AcquisitionEngine(self._config, self._session, parent=self)

        # Quiet background reachability checks → the status-bar network dots.
        self._network_monitor = NetworkMonitor(self._config, parent=self)
        self._network_monitor.state_changed.connect(self._status.set_network)
        self._network_monitor.start()

        self._connection = ConnectionPage(self._config)
        self._acquisition = ImagingPage(self._config, self._session, self._engine)
        self._sequencer = SequencerPage(self._config, self._session, self._engine)
        self._configuration = ConfigurationPage(self._config)
        self._analyze = AnalyzeScreen(self._config)
        self._wire_observation_identity()

        # NINA-style: the night happens in Capture; Equipment is setup, the
        # Sequencer plans/runs the night (usable offline for plan editing),
        # Analyze is post-prod. (Mode id "connect" kept for config back-compat.)
        self._pages: dict[str, QWidget] = {
            "connect": self._connection,
            "capture": self._acquisition,
            "sequencer": self._sequencer,
            "analyze": self._analyze,
            "settings": self._configuration,
        }

        self._page_indices: dict[str, int] = {
            mode_id: self._stack.addWidget(page) for mode_id, page in self._pages.items()
        }

        # Connection/activity state feeding the sidebar phase dots.
        self._conn_state: dict[str, str] = dict.fromkeys(
            ("mount", "camera", "filterwheel", "focuser"), "disconnected"
        )
        self._sequence_active = False
        self._autofocus_active = False

        self._wire_pages()

    def _wire_observation_identity(self) -> None:
        """Keep Capture and Plan on one observation object source of truth."""
        camera = self._acquisition._camera_dock
        plan = self._sequencer.panel
        camera.object_name_changed.connect(plan.set_object_name)
        plan.object_name_changed.connect(self._on_sequence_object_changed)
        self._acquisition.target_coordinates_changed.connect(self._sequencer.set_target_coordinates)
        self._sequencer.target_resolved.connect(self._acquisition.set_catalogue_target)
        self._sequencer.solve_for_run_requested.connect(self._acquisition.set_solve_for_next_run)
        self._sequencer.science_source_resolved.connect(self._acquisition.set_science_source)

    def _on_sequence_object_changed(self, object_name: str) -> None:
        camera = self._acquisition._camera_dock
        camera.set_object_name(object_name)
        self._engine.on_object_changed()

    # ------------------------------------------------------------------
    # Menus
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        bar = self.menuBar()

        # Desktop convention: saved data is always reachable from the first
        # menu, whether the observer is currently connecting equipment,
        # reviewing a prior run, or looking at a live frame.
        file_menu = bar.addMenu("File")
        open_fits = QAction("Open FITS image…", self)
        open_fits.setShortcut(QKeySequence.StandardKey.Open)
        open_fits.setToolTip("Open a saved FITS image in Capture; then use Field → Identify field.")
        open_fits.triggered.connect(self._open_fits_from_menu)
        file_menu.addAction(open_fits)
        open_session = QAction("Open session photometry…", self)
        open_session.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_session.setToolTip("Open the saved differential-photometry measurements of a session")
        open_session.triggered.connect(self._open_session_photometry_from_menu)
        file_menu.addAction(open_session)
        file_menu.addSeparator()
        quit_action = QAction("Quit Argos", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Small, intentionally plain resource menu next to File: opening the
        # manual or checking the version must not require hunting through a
        # scientific workflow menu.
        more_menu = bar.addMenu("More")
        documentation = QAction("Documentation & website", self)
        documentation.setToolTip("Open the Argos documentation and project website")
        documentation.triggered.connect(self._open_project_website)
        more_menu.addAction(documentation)
        support_bundle = QAction("Create local support bundle…", self)
        support_bundle.setToolTip(
            "Create a local ZIP of redacted logs and optionally session metadata. Nothing is uploaded."
        )
        support_bundle.triggered.connect(self._create_support_bundle)
        more_menu.addAction(support_bundle)
        more_menu.addSeparator()
        about = QAction("About & credits", self)
        about.triggered.connect(self._show_about)
        more_menu.addAction(about)

        view = bar.addMenu("View")
        # Derived from the sidebar's MODES tuple — one source of truth, so a
        # phase insertion can't silently desynchronise the F-key bindings.
        for i, (mode_id, label, _tooltip) in enumerate(MODES):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(f"F{i + 1}"))
            action.triggered.connect(lambda _c, m=mode_id: self._sidebar.select(m))
            view.addAction(action)

        view.addSeparator()
        reset = QAction("Reset Window Layout", self)
        reset.triggered.connect(self._reset_layout)
        view.addAction(reset)

        # Field identification actions describe the observer's outcome. The
        # ASTAP/plate-solving implementation remains visible in the tooltip.
        astrometry = bar.addMenu("Field")
        solve = QAction("Identify field", self)
        solve.setToolTip("Find coordinates and scale for the current image (ASTAP)")
        solve.triggered.connect(lambda: self._engine.solve_now())
        astrometry.addAction(solve)
        self._auto_solve_action = QAction("Keep field identified automatically", self)
        self._auto_solve_action.setCheckable(True)
        self._auto_solve_action.setToolTip(
            "Refresh field coordinates when needed so the grid and markers follow the field.\n"
            "It is enabled automatically during a sequence."
        )
        self._auto_solve_action.toggled.connect(self._engine.astrometry.set_auto)
        # Sequence start arms the auto-solve; this checkable action is the
        # single UI for it, so the arming routes through it (→ set_auto).
        self._acquisition.auto_solve_armed.connect(self._auto_solve_action.setChecked)
        astrometry.addAction(self._auto_solve_action)
        astrometry.addSeparator()
        plate = QAction("Configure field identification…", self)
        plate.triggered.connect(lambda: self._open_astrometry_settings(SECTION_ASTROMETRY))
        astrometry.addAction(plate)
        catalog = QAction("Configure catalogues…", self)
        catalog.triggered.connect(lambda: self._open_astrometry_settings(SECTION_CATALOG))
        astrometry.addAction(catalog)

        # Photometry owns field-star selection and the live diagnostics. Field
        # identification/ASTAP setup stays in the separate Field menu.
        photometry = bar.addMenu("Photometry")
        select_stars = QAction("Photometry setup…", self)
        select_stars.setToolTip(
            "Choose a variable-star target and review the comparison-star ensemble in this field"
        )
        select_stars.triggered.connect(
            lambda: self._acquisition.open_photometry(select_variable=True)
        )
        photometry.addAction(select_stars)
        curve = QAction("Live curves & diagnostics…", self)
        curve.setToolTip("Open live differential light curves, image quality and selected stars")
        curve.triggered.connect(lambda: self._acquisition.open_photometry())
        photometry.addAction(curve)
        rerun = QAction("Re-run subs…", self)
        rerun.setToolTip("Re-run differential photometry over a folder of saved subs")
        rerun.triggered.connect(lambda: self._acquisition.rerun_subs())
        photometry.addAction(rerun)

    def _open_fits_from_menu(self) -> None:
        self._sidebar.select("capture")
        self._acquisition.open_fits()

    def _open_session_photometry_from_menu(self) -> None:
        self._sidebar.select("analyze")
        self._analyze.open_session_photometry()

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _wire_signals(self) -> None:
        self._sidebar.mode_changed.connect(self._on_mode_changed)
        self._status.badge_clicked.connect(self._on_badge_clicked)

    def _wire_pages(self) -> None:
        # Acquisition page → global status bar.
        self._acquisition.device_state_changed.connect(self._on_device_state_changed)
        self._acquisition.tracking_changed.connect(self._status.set_tracking)
        self._acquisition.action_changed.connect(self._status.set_action)

        # Capture visibility on every screen: sequence progress + LIVE chip in
        # the top strip, activity dots on the sidebar (WS4). The run lives on
        # the Sequencer page now, so the strip listens to the engine directly.
        self._engine.sequence_running.connect(self._on_sequence_running)
        self._engine.sequence_progress.connect(self._status.set_sequence_progress)
        self._acquisition.hfd_updated.connect(self._status.set_hfd)
        self._acquisition.camera_state_changed.connect(self._on_camera_state)
        self._acquisition.autofocus_state.connect(self._on_autofocus_state)
        self._status.capture_clicked.connect(lambda: self._sidebar.select("capture"))

        # Connection intents → device session (dict-dispatched per device).
        self._connection.discover_requested.connect(self._session.discover)
        self._connection.connect_requested.connect(self._session.connect_device)
        self._connection.disconnect_requested.connect(self._session.disconnect_device)
        self._connection.connect_all_requested.connect(self._session.connect_all)
        self._connection.disconnect_all_requested.connect(self._session.disconnect_all)
        self._connection.telescope_profile_requested.connect(self._on_telescope_profile_requested)
        self._configuration.set_telescope_profile_applier(self._on_telescope_profile_requested)
        self._session.discovered_address.connect(self._connection.set_discovered_address)
        self._session.mount_mode.connect(self._status.set_mount_mode)

        # Stellarium card (on the Connection page) ↔ the session-owned server.
        card = self._connection.stellarium_card
        card.start_server_requested.connect(self._session.start_stellarium)
        card.stop_server_requested.connect(self._session.stop_stellarium)
        card.online_target_lookup_changed.connect(
            lambda enabled: self._config.set("stellarium.online_target_lookup", enabled)
        )
        self._session.stellarium_state.connect(card.set_server_state)
        self._session.stellarium_clients.connect(card.set_client_count)
        self._session.stellarium_target.connect(self._on_stellarium_target)
        self._session.stellarium_error.connect(self._on_stellarium_error)

    # ------------------------------------------------------------------
    # Stellarium fan-out (the session owns the server worker)
    # ------------------------------------------------------------------

    def _on_stellarium_target(self, ra_hours: float, dec_degrees: float) -> None:
        self._connection.stellarium_card.flash_goto(ra_hours, dec_degrees)
        self._acquisition.goto_target(ra_hours, dec_degrees, label="goto")
        self._acquisition.suggest_target_from_coordinates(
            ra_hours,
            dec_degrees,
            allow_network=bool(self._config.get("stellarium.online_target_lookup", False)),
        )

    def _on_telescope_profile_requested(self, key: str) -> None:
        """Apply one telescope profile for all views, or restore both selectors."""
        self._connection.apply_telescope_profile(key)
        self._configuration.sync_telescope_profile()

    def _on_stellarium_error(self, message: str) -> None:
        self._connection.stellarium_card.set_server_state(False, "✗  error")

    # ------------------------------------------------------------------
    # Status fan-out
    # ------------------------------------------------------------------

    def _on_device_state_changed(self, device_id: str, state: str, info: str) -> None:
        self._status.set_device_state(device_id, state, info)
        self._connection.set_device_state(device_id, state, info)

        self._conn_state[device_id] = state
        self._refresh_sidebar_states()

    def _on_sequence_running(self, running: bool) -> None:
        self._sequence_active = running
        self._status.set_sequence_running(running)
        self._refresh_sidebar_states()

    def _on_autofocus_state(self, running: bool) -> None:
        self._autofocus_active = running
        self._refresh_sidebar_states()

    def _on_camera_state(self, state: CameraState) -> None:
        self._status.set_live(state is CameraState.LIVE)

    def _refresh_sidebar_states(self) -> None:
        """Recompute the sidebar mode dots from device + activity state.

        ready = prerequisites met, active = running now, blocked = a required
        device is missing. Equipment/Analyze/Settings stay neutral — they are
        always usable.
        """
        camera_up = self._conn_state["camera"] in ("connected", "busy")
        if self._autofocus_active:
            self._sidebar.set_mode_state("capture", "active")
        else:
            self._sidebar.set_mode_state("capture", "ready" if camera_up else "blocked")
        # The run belongs to the Sequencer mode; one glowing dot, not two.
        self._sidebar.set_mode_state("sequencer", "active" if self._sequence_active else "none")

    def _on_mode_changed(self, mode_id: str) -> None:
        index = self._page_indices.get(mode_id)
        if index is None:
            return
        self._stack.setCurrentIndex(index)
        self._config.set(_CFG_MODE, mode_id)
        logger.debug("Switched to mode: %s", mode_id)

    def _on_badge_clicked(self, device_id: str) -> None:
        if self._status.device_state(device_id) == "disconnected":
            self._sidebar.select("connect")

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def status(self) -> TopStatusBar:
        return self._status

    @property
    def sidebar(self) -> Sidebar:
        return self._sidebar

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        geo = self._config.get(_CFG_GEOMETRY)
        state = self._config.get(_CFG_STATE)
        if geo:
            try:
                self.restoreGeometry(QByteArray(base64.b64decode(geo)))
            except Exception as exc:
                logger.warning("restoreGeometry failed: %s", exc)
        if state:
            try:
                self.restoreState(QByteArray(base64.b64decode(state)))
            except Exception as exc:
                logger.warning("restoreState failed: %s", exc)

    def _save_state(self) -> None:
        self._config.set(_CFG_GEOMETRY, base64.b64encode(bytes(self.saveGeometry())).decode())
        self._config.set(_CFG_STATE, base64.b64encode(bytes(self.saveState())).decode())
        # The Capture page owns its own dockable workspace (WS9a) — persist it
        # alongside the shell's own dock state.
        self._acquisition.save_layout()
        self._sequencer.save_layout()
        self._analyze.save_layout()

    def _reset_layout(self) -> None:
        self._config.set(_CFG_GEOMETRY, None)
        self._config.set(_CFG_STATE, None)
        self._config.set("ui.sequencer.layout", None)
        self._config.set("ui.review.layout", None)
        self.statusBar().showMessage("Window layout will reset on next launch.", 4000)

    # ------------------------------------------------------------------
    # Menu actions
    # ------------------------------------------------------------------

    def _open_astrometry_settings(self, section: str) -> None:
        """Open the shared astrometry/catalog settings dialog on *section*.

        On save the engine drops its per-field catalog cache and re-queries with
        the new parameters, so a magnitude-limit change (etc.) reaches the live
        field on the spot rather than only on the next slew/solve.
        """
        dialog = AstrometrySettingsDialog(self._config, self, section=section)
        dialog.saved.connect(self._engine.refetch_catalog)
        dialog.exec()

    def _show_about(self) -> None:
        """Show the branded About window with credits and project links."""
        self._build_about_dialog().exec()

    def _create_support_bundle(self) -> None:
        """Create an explicitly local, privacy-preserving support ZIP."""
        include_session = (
            QMessageBox.question(
                self,
                "Include session metadata?",
                "Include one session's JSON, JSONL and CSV files? Raw FITS, "
                "site coordinates and network addresses are excluded. The ZIP stays "
                "on this computer until you choose to share it.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            == QMessageBox.StandardButton.Yes
        )
        session_dir: Path | None = None
        if include_session:
            selected = QFileDialog.getExistingDirectory(
                self, "Choose an Argos session", str(self._config.sessions_path)
            )
            if not selected:
                return
            session_dir = Path(selected)

        desktop = Path.home() / "Desktop"
        start = desktop if desktop.is_dir() else Path.home()
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save local support bundle",
            str(start / f"argos-support-{self.APP_VERSION}.zip"),
            "ZIP archive (*.zip)",
        )
        if not filename:
            return
        summary = {
            "hardware_profile": self._config.get("hardware.profile", "unknown"),
            "log_level": self._config.get("ui.log_level", "INFO"),
            "local_diagnostics_enabled": bool(self._config.get("diagnostics.enabled", False)),
            "astap_configured": bool(self._config.get("astrometry.astap_path", "")),
        }
        try:
            bundle = create_support_bundle(
                filename,
                log_directory=diagnostics_directory(),
                session_directory=session_dir,
                config_summary=summary,
            )
        except OSError as exc:
            logger.exception("Could not create local support bundle")
            QMessageBox.warning(self, "Support bundle", f"Could not create the ZIP:\n{exc}")
            return
        QMessageBox.information(
            self,
            "Local support bundle created",
            f"Created {bundle.path.name} with {len(bundle.files)} file(s).\n\n"
            "Argos did not upload it. Review the ZIP before sharing it.",
        )

    def _build_about_dialog(self) -> QDialog:
        """Create the About dialog separately so it stays easy to inspect/test."""
        dialog = QDialog(self)
        dialog.setObjectName("about_dialog")
        dialog.setWindowTitle("About Argos")
        dialog.setMinimumWidth(460)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(10)

        logo = QLabel()
        logo.setObjectName("about_logo")
        logo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        image = QPixmap(str(Path(__file__).parent / "assets" / "logo.png"))
        if not image.isNull():
            logo.setPixmap(
                image.scaled(
                    104,
                    104,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        layout.addWidget(logo)

        title = QLabel("Argos")
        title.setObjectName("about_title")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        title.setStyleSheet("font-size: 26px; font-weight: 700; background: transparent;")
        layout.addWidget(title)

        version = QLabel(f"Version {self.APP_VERSION}")
        version.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        version.setStyleSheet("background: transparent;")
        layout.addWidget(version)

        description = QLabel(
            "Scientific acquisition and differential photometry for ZWO Seestar telescopes."
        )
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        description.setStyleSheet("background: transparent;")
        layout.addWidget(description)

        credits = QLabel(
            "<b>Credits</b><br>"
            "Created and maintained by Jules Perret.<br><br>"
            "Built with Python, PyQt6, NumPy, Astropy, pyqtgraph and ASCOM Alpaca.<br><br>"
            "Copyright © 2026 Argos contributors · GNU GPL v3 or later."
        )
        credits.setObjectName("about_credits")
        credits.setWordWrap(True)
        credits.setTextFormat(Qt.TextFormat.RichText)
        credits.setStyleSheet("background: transparent;")
        layout.addWidget(credits)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        website = buttons.addButton(
            "Documentation & website", QDialogButtonBox.ButtonRole.ActionRole
        )
        website.clicked.connect(self._open_project_website)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        return dialog

    def _open_project_website(self) -> None:
        """Open the public manual/project page in the user's web browser."""
        if not QDesktopServices.openUrl(QUrl(_PROJECT_URL)):
            self.statusBar().showMessage("Could not open the Argos website.", 4000)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        # Order matters: stop the capture workers first (they hold device
        # references), then the session's pollers + Stellarium server.
        self._configuration.shutdown()
        self._sequencer.shutdown()
        self._acquisition.shutdown()
        self._session.shutdown()
        self._network_monitor.stop()
        # A check in flight can hold the thread for up to three connect
        # timeouts (host + two internet targets) — wait long enough.
        self._network_monitor.wait(6000)
        self._save_state()
        self._config.save()
        logger.info("Shell closed")
        super().closeEvent(event)
