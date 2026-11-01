"""DeviceSession — owns the Alpaca device handles and their pollers.

Extracted from the ImagingPage (WS5): the session is a plain ``QObject``
living on the **UI thread** (never ``moveToThread`` it — every worker→UI
connection relies on the receiver's thread affinity for queued delivery).
It owns:

- the four device handles (Telescope, Camera, Focuser, FilterWheel) and
  their connect/disconnect lifecycles;
- Alpaca UDP discovery;
- the mount-position poller and both temperature pollers;
- the pointing intent (``target_radec``, set on goto) and the cached
  ``last_position``;
- the Stellarium telescope-control server worker (fed the mount position
  directly — no Shell-level signal plumbing).

The UI consumes typed signals (``capabilities_ready`` carries a frozen
:class:`CameraCapabilities`, …); device *use* (exposures, sweeps, sequences)
belongs to the AcquisitionEngine, which coordinates with the session through
the ``camera_will_disconnect`` / ``focuser_will_disconnect`` hooks.
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal, pyqtSlot

from argos.core.alpaca.camera import Camera
from argos.core.alpaca.client import AlpacaError
from argos.core.alpaca.filterwheel import FilterWheel, position_names
from argos.core.alpaca.focuser import Focuser
from argos.core.alpaca.telescope import MountPosition, Telescope
from argos.core.config import Config
from argos.core.hardware import active as hardware, detect
from argos.core.session.types import CameraCapabilities, FilterWheelState, FocuserState
from argos.workers.discovery_worker import DiscoveryWorker
from argos.workers.polling_worker import MountPollingWorker, TemperaturePollingWorker
from argos.workers.stellarium_worker import StellariumWorker

logger = logging.getLogger(__name__)


class _JogRunnable(QRunnable):
    """One-shot off-thread MoveAxis call.

    The first Alpaca call on a fresh TCP connection takes ~600ms. Running it
    on the main thread would (a) freeze the UI and (b) consume the
    button-released Qt event before returning — causing an instant stop with
    zero visible movement. This QRunnable fixes both problems.
    """

    def __init__(self, telescope, axis: int, rate: float, log_signal) -> None:
        super().__init__()
        self._telescope = telescope
        self._axis = axis
        self._rate = rate
        self._log = log_signal

    def run(self) -> None:
        try:
            self._telescope.move_axis(self._axis, self._rate)
        except AlpacaError as exc:
            action = "Stop jog" if self._rate == 0.0 else "Jog"
            level = "WARN" if self._rate == 0.0 else "ERROR"
            self._log.emit(level, f"{action}: {exc}")


class _FilterMoveRunnable(QRunnable):
    """One-shot off-thread filter-wheel move + settle poll.

    The move is async (the wheel reports position -1 while turning). Running it
    off the UI thread keeps the UI responsive; the result is reported back via
    the ``done`` signal as ``(position, name)``.
    """

    def __init__(self, filterwheel, position: int, done_signal, log_signal) -> None:
        super().__init__()
        self._fw = filterwheel
        self._position = position
        self._done = done_signal
        self._log = log_signal

    def run(self) -> None:
        try:
            self._fw.set_position(self._position)
            deadline = time.monotonic() + 20.0
            while self._fw.get_position() == -1 and time.monotonic() < deadline:
                time.sleep(0.15)
            pos = self._fw.get_position()
            self._done.emit(pos, self._fw.position_name())
        except AlpacaError as exc:
            self._log.emit("ERROR", f"Filter move: {exc}")


class DeviceSession(QObject):
    """Device handles, connect order and pollers — the hardware session."""

    # Shell-facing primitives (signatures identical to the old ImagingPage
    # upward signals, so the status bar / Connection page wiring is unchanged).
    device_state_changed = pyqtSignal(str, str, str)  # device, state, info
    tracking_changed = pyqtSignal(object)  # bool | None
    action_changed = pyqtSignal(str)
    log_message = pyqtSignal(str, str)  # level, message
    discovered_address = pyqtSignal(str, int)  # host, port

    # Typed device state.
    mount_position = pyqtSignal(object)  # MountPosition
    mount_mode = pyqtSignal(object)  # str | None — "Alt-Az" / "EQ" / "EQ (GEM)"
    mount_lost = pyqtSignal()
    capabilities_ready = pyqtSignal(object)  # CameraCapabilities
    filterwheel_state = pyqtSignal(object)  # FilterWheelState
    focuser_state = pyqtSignal(object)  # FocuserState
    filter_moved = pyqtSignal(int, str)  # wheel settled at (pos, name)
    camera_temperature = pyqtSignal(object)  # float | None
    focuser_temperature = pyqtSignal(object)  # float | None
    slewed = pyqtSignal()  # a goto was issued — the previous plate solve is stale

    # Pre-disconnect hooks — emitted synchronously (same thread) so the
    # AcquisitionEngine can stop a preview loop / autofocus sweep *before*
    # the device handle is dropped (order matters: a worker mid-exposure
    # against a disconnected camera is an AlpacaError storm).
    camera_will_disconnect = pyqtSignal()
    focuser_will_disconnect = pyqtSignal()

    # Stellarium relays.
    stellarium_target = pyqtSignal(float, float)  # ra_hours, dec_degrees
    stellarium_clients = pyqtSignal(int)
    stellarium_state = pyqtSignal(bool)
    stellarium_error = pyqtSignal(str)

    _filter_settled = pyqtSignal(int, str)  # internal: from the pool runnable

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config

        self._telescope: Telescope | None = None
        self._camera: Camera | None = None
        self._focuser: Focuser | None = None
        self._filterwheel: FilterWheel | None = None

        self._discovery: DiscoveryWorker | None = None
        self._polling: MountPollingWorker | None = None
        self._cam_temp_poll: TemperaturePollingWorker | None = None
        self._foc_temp_poll: TemperaturePollingWorker | None = None
        self._stellarium: StellariumWorker | None = None

        self._last_position: MountPosition | None = None
        # Pointing intent — one tuple so worker-thread readers (the sequence
        # frame-context provider) see an atomic reference swap.
        self._target_radec: tuple[float, float] | None = None

        # WS8: after a network drop the mount reconnects itself — a dead
        # polling loop must not end the night. Cleared on user disconnect.
        self._mount_endpoint: tuple[str, int] | None = None
        self._mount_retry = QTimer(self)
        self._mount_retry.setInterval(10_000)
        self._mount_retry.timeout.connect(self._retry_mount)

        self._filter_settled.connect(self._on_filter_settled)

    # ------------------------------------------------------------------
    # Handles + cached state
    # ------------------------------------------------------------------

    @property
    def telescope(self) -> Telescope | None:
        return self._telescope

    @property
    def camera(self) -> Camera | None:
        return self._camera

    @property
    def focuser(self) -> Focuser | None:
        return self._focuser

    @property
    def filterwheel(self) -> FilterWheel | None:
        return self._filterwheel

    @property
    def last_position(self) -> MountPosition | None:
        """Latest polled mount position (an atomic reference swap — safe to
        read from worker threads as a single snapshot)."""
        return self._last_position

    @property
    def target_radec(self) -> tuple[float, float] | None:
        """The last goto target ``(ra_hours, dec_degrees)``, or ``None``."""
        return self._target_radec

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> None:
        if self._discovery and self._discovery.isRunning():
            return
        self.log_message.emit("INFO", "Starting Alpaca discovery…")
        # Fallback candidates when the UDP broadcast is silent (hotspots
        # block it): the last-used host, then the Seestar's AP-mode address.
        candidates = (self._config.alpaca_host, hardware.profile().ap_host)
        self._discovery = DiscoveryWorker(
            port=self._config.alpaca_port, candidates=candidates, timeout=8.0, parent=self
        )
        self._discovery.devices_found.connect(self._on_devices_found)
        self._discovery.error_occurred.connect(self._on_discovery_error)
        self._discovery.start()

    def _on_devices_found(self, devices) -> None:
        if not devices:
            self.log_message.emit(
                "WARN", "No Alpaca devices found (broadcast, direct probes and subnet scan)."
            )
            return
        first = devices[0]
        self.log_message.emit("OK", f"Found {first.host}:{first.port}")
        self.discovered_address.emit(first.host, first.port)

    def _on_discovery_error(self, message: str) -> None:
        self.log_message.emit("ERROR", f"Discovery: {message}")

    # ------------------------------------------------------------------
    # Connect / disconnect — dict-dispatched (replaces the Shell if/elif)
    # ------------------------------------------------------------------

    def connect_device(self, device_id: str, host: str, port: int) -> None:
        handler = {
            "mount": self.connect_mount,
            "camera": self.connect_camera,
            "filterwheel": self.connect_filterwheel,
            "focuser": self.connect_focuser,
        }.get(device_id)
        if handler is None:
            self.action_changed.emit(f"{device_id.title()} connect — not implemented yet")
            return
        handler(host, port)

    def disconnect_device(self, device_id: str) -> None:
        handler = {
            "mount": self.disconnect_mount,
            "camera": self.disconnect_camera,
            "filterwheel": self.disconnect_filterwheel,
            "focuser": self.disconnect_focuser,
        }.get(device_id)
        if handler is not None:
            handler()

    def connect_all(self, host: str, port: int) -> None:
        self.connect_mount(host, port)
        self.connect_camera(host, port)
        self.connect_filterwheel(host, port)
        self.connect_focuser(host, port)

    def disconnect_all(self) -> None:
        self.disconnect_camera()
        self.disconnect_mount()
        self.disconnect_filterwheel()
        self.disconnect_focuser()
        self.action_changed.emit("Disconnected")

    def connect_mount(self, host: str, port: int) -> None:
        self._config.alpaca_host = host
        self._config.alpaca_port = port
        self.action_changed.emit(f"Connecting mount {host}:{port}…")
        try:
            scope = Telescope(host=host, port=port)
            name = scope.connect()
            self._telescope = scope
            self._mount_endpoint = (host, port)
            self._mount_retry.stop()
            self.log_message.emit("OK", f"Mount connected: {name}")
            if scope.equatorial_system:
                self.log_message.emit(
                    "INFO",
                    f"Mount coordinates: {scope.equatorial_system} → Argos J2000 conversion enabled.",
                )
            else:
                self.log_message.emit(
                    "WARN",
                    "Mount coordinate system unavailable; assuming the driver accepts J2000.",
                )
            if scope.clock_error_seconds is not None and scope.clock_error_seconds > 60.0:
                self.log_message.emit(
                    "ERROR",
                    "Mount clock is %.1f h off — correct date/time in the Seestar app; GoTo and Sync are disabled."
                    % (scope.clock_error_seconds / 3600.0),
                )
            self.device_state_changed.emit("mount", "connected", name)
            self.mount_mode.emit(scope.alignment_mode)
            self._start_polling()
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Mount: {exc}")
            self.device_state_changed.emit("mount", "error", str(exc)[:48])

    @pyqtSlot()
    def _retry_mount(self) -> None:
        """One auto-reconnect attempt (every 10s after a lost connection)."""
        if self._telescope is not None or self._mount_endpoint is None:
            self._mount_retry.stop()
            return
        host, port = self._mount_endpoint
        try:
            scope = Telescope(host=host, port=port)
            name = scope.connect()
        except AlpacaError as exc:
            logger.info("Mount reconnect attempt failed: %s", exc)
            return  # timer keeps firing
        self._telescope = scope
        self._mount_retry.stop()
        self.log_message.emit("OK", f"Mount reconnected: {name}")
        if scope.equatorial_system:
            self.log_message.emit(
                "INFO",
                f"Mount coordinates: {scope.equatorial_system} → Argos J2000 conversion enabled.",
            )
        else:
            self.log_message.emit(
                "WARN",
                "Mount coordinate system unavailable; assuming the driver accepts J2000.",
            )
        if scope.clock_error_seconds is not None and scope.clock_error_seconds > 60.0:
            self.log_message.emit(
                "ERROR",
                "Mount clock is %.1f h off — correct date/time in the Seestar app; GoTo and Sync are disabled."
                % (scope.clock_error_seconds / 3600.0),
            )
        self.device_state_changed.emit("mount", "connected", name)
        self.mount_mode.emit(scope.alignment_mode)
        self._start_polling()

    def disconnect_mount(self) -> None:
        # Explicit user intent — never fight it with the auto-reconnect.
        self._mount_retry.stop()
        self._mount_endpoint = None
        self._stop_polling()
        if self._telescope:
            try:
                self._telescope.disconnect()
            except AlpacaError:
                pass
            self._telescope = None
        self.device_state_changed.emit("mount", "disconnected", "")
        self.tracking_changed.emit(None)
        self.mount_mode.emit(None)
        self.log_message.emit("INFO", "Mount disconnected.")

    def connect_camera(self, host: str, port: int) -> None:
        self._config.alpaca_host = host
        self._config.alpaca_port = port
        self.action_changed.emit(f"Connecting camera {host}:{port}…")
        try:
            cam = Camera(host=host, port=port)
            name = cam.connect()
            self._camera = cam
            self._check_profile_against(cam, name)
            self._publish_camera_capabilities(cam)
            self._start_camera_temp_poll(cam)
            self.log_message.emit("OK", f"Camera connected: {name}")
            self.device_state_changed.emit("camera", "connected", name)
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Camera: {exc}")
            self.device_state_changed.emit("camera", "error", str(exc)[:48])

    def _check_profile_against(self, cam: Camera, camera_name: str) -> None:
        """Warn when the connected camera contradicts the active profile.

        Advisory only — the profile is never switched here. Changing the plate
        scale under a running session would be a worse surprise than the
        mismatch, so the observer is told and decides in Settings.
        """
        scope = hardware.profile()
        found = detect.mismatches(
            scope, camera_name=camera_name, width=cam.width, height=cam.height
        )
        if not found:
            return

        for note in found:
            logger.warning("Telescope profile mismatch: %s", note)
            self.log_message.emit("WARN", f"Profile mismatch — {note}")

        better = detect.suggest(camera_name=camera_name, width=cam.width, height=cam.height)
        if better is not None and better.key != scope.key:
            self.log_message.emit(
                "WARN", f"This looks like a {better.name} — change it in Settings → Telescope."
            )

    def _publish_camera_capabilities(self, cam: Camera) -> None:
        """Read the driver-derived limits + optional parameters (offset /
        binning) the driver proves it supports, and publish them typed."""
        offset = cam.get_offset()
        self.capabilities_ready.emit(
            CameraCapabilities(
                gain_min=cam.gain_min,
                gain_max=cam.gain_max,
                exposure_min=cam.exposure_min,
                exposure_max=cam.exposure_max,
                offset_min=cam.offset_min,
                offset_max=cam.offset_max,
                offset=offset,
                max_bin=cam.max_bin,
                binning=cam.get_binning() if cam.max_bin > 1 else 1,
            )
        )
        self.log_message.emit(
            "INFO",
            f"Camera limits: gain {cam.gain_min}–{cam.gain_max}, "
            f"exposure {cam.exposure_min:g}–{cam.exposure_max:g}s"
            + (", offset" if offset is not None else "")
            + (f", bin ≤{cam.max_bin}" if cam.max_bin > 1 else ""),
        )

    def disconnect_camera(self) -> None:
        # Synchronous hook: the engine releases the preview loop first.
        self.camera_will_disconnect.emit()
        self._stop_camera_temp_poll()
        if self._camera:
            try:
                self._camera.disconnect()
            except AlpacaError:
                pass
            self._camera = None
        self.device_state_changed.emit("camera", "disconnected", "")
        self.log_message.emit("INFO", "Camera disconnected.")

    def connect_filterwheel(self, host: str, port: int) -> None:
        self._config.alpaca_host = host
        self._config.alpaca_port = port
        self.action_changed.emit(f"Connecting filter wheel {host}:{port}…")
        try:
            fw = FilterWheel(host=host, port=port)
            fw.connect()
            self._filterwheel = fw
            slots = position_names()
            names = tuple(slots[i] for i in sorted(slots))
            pos = fw.get_position()
            self.filterwheel_state.emit(
                FilterWheelState(names=names, position=pos, position_name=fw.position_name())
            )
            self.log_message.emit("OK", f"Filter wheel connected — {fw.position_name()}")
            self.device_state_changed.emit("filterwheel", "connected", fw.position_name())
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Filter wheel: {exc}")
            self.device_state_changed.emit("filterwheel", "error", str(exc)[:48])

    def disconnect_filterwheel(self) -> None:
        if self._filterwheel:
            try:
                self._filterwheel.disconnect()
            except AlpacaError:
                pass
            self._filterwheel = None
        self.device_state_changed.emit("filterwheel", "disconnected", "")
        self.log_message.emit("INFO", "Filter wheel disconnected.")

    def connect_focuser(self, host: str, port: int) -> None:
        self._config.alpaca_host = host
        self._config.alpaca_port = port
        self.action_changed.emit(f"Connecting focuser {host}:{port}…")
        try:
            foc = Focuser(host=host, port=port)
            name = foc.connect()
            self._focuser = foc
            pos = foc.get_position()
            self.focuser_state.emit(
                FocuserState(
                    name=name,
                    position=pos,
                    max_step=foc.max_step,
                    max_increment=foc.max_increment,
                    absolute=foc.absolute,
                )
            )
            # Periodic refresh — a temperature read once at connect would go
            # stale over the night (temp-based AF triggers need it live).
            self._stop_focuser_temp_poll()  # reconnect must not leak a poller
            self._foc_temp_poll = TemperaturePollingWorker(foc.get_temperature, parent=self)
            self._foc_temp_poll.temperature_updated.connect(self.focuser_temperature)
            self._foc_temp_poll.start()
            self.log_message.emit("OK", f"Focuser connected: {name}  pos={pos}")
            self.device_state_changed.emit("focuser", "connected", name)
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Focuser: {exc}")
            self.device_state_changed.emit("focuser", "error", str(exc)[:48])

    def disconnect_focuser(self) -> None:
        # Synchronous hook: the engine stops a running autofocus sweep first.
        self.focuser_will_disconnect.emit()
        self._stop_focuser_temp_poll()
        if self._focuser:
            try:
                self._focuser.disconnect()
            except AlpacaError:
                pass
            self._focuser = None
        self.device_state_changed.emit("focuser", "disconnected", "")
        self.log_message.emit("INFO", "Focuser disconnected.")

    # ------------------------------------------------------------------
    # Pollers
    # ------------------------------------------------------------------

    def _start_camera_temp_poll(self, cam: Camera) -> None:
        self._stop_camera_temp_poll()  # reconnect without disconnect must not leak a poller
        self._cam_temp_poll = TemperaturePollingWorker(cam.get_ccd_temperature, parent=self)
        self._cam_temp_poll.temperature_updated.connect(self.camera_temperature)
        self._cam_temp_poll.start()

    def _stop_camera_temp_poll(self) -> None:
        if self._cam_temp_poll and self._cam_temp_poll.isRunning():
            self._cam_temp_poll.stop()
            self._cam_temp_poll.wait(3000)
        self._cam_temp_poll = None

    def _stop_focuser_temp_poll(self) -> None:
        if self._foc_temp_poll and self._foc_temp_poll.isRunning():
            self._foc_temp_poll.stop()
            self._foc_temp_poll.wait(3000)
        self._foc_temp_poll = None

    def _start_polling(self) -> None:
        if not self._telescope:
            return
        self._polling = MountPollingWorker(self._telescope, parent=self)
        self._polling.position_updated.connect(self._on_position)
        self._polling.error_occurred.connect(self._on_poll_error)
        self._polling.connection_lost.connect(self._on_mount_lost)
        self._polling.start()

    def _stop_polling(self) -> None:
        if self._polling and self._polling.isRunning():
            self._polling.stop()
            self._polling.wait(3000)
        self._polling = None

    def _on_poll_error(self, message: str) -> None:
        self.log_message.emit("WARN", f"Poll: {message}")

    @pyqtSlot(object)
    def _on_position(self, pos: MountPosition) -> None:
        self._last_position = pos
        self.mount_position.emit(pos)
        self.tracking_changed.emit(pos.tracking)
        # Feed the Stellarium server directly so its on-screen reticle keeps
        # following the live mount position (no-op while it isn't running).
        if self._stellarium is not None:
            self._stellarium.update_mount_position(pos.ra, pos.dec, pos.slewing)
        if pos.slewing:
            self.device_state_changed.emit("mount", "busy", "slewing")
        else:
            self.device_state_changed.emit("mount", "connected", "")

    def _on_mount_lost(self) -> None:
        self.log_message.emit("ERROR", "Mount connection lost.")
        self._stop_polling()
        self._telescope = None
        self.mount_lost.emit()
        self.mount_mode.emit(None)
        self.device_state_changed.emit("mount", "error", "")
        self.tracking_changed.emit(None)
        if self._mount_endpoint is not None:
            self.log_message.emit("INFO", "Retrying the mount every 10s — disconnect to stop.")
            self._mount_retry.start()

    # ------------------------------------------------------------------
    # Mount commands
    # ------------------------------------------------------------------

    def goto(self, ra_h: float, dec_d: float) -> None:
        if not self._telescope:
            return
        try:
            self._telescope.set_tracking(True)
            self._telescope.slew_to(ra_h, dec_d)
            self._target_radec = (ra_h, dec_d)
            self.slewed.emit()  # the slew invalidates the previous solve
            self.log_message.emit("CMD", f"Slewing → RA {ra_h:.4f}h Dec {dec_d:+.4f}°")
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Goto: {exc}")

    def sync_current(self) -> None:
        if not (self._telescope and self._last_position):
            return
        ra, dec = self._last_position.ra, self._last_position.dec
        try:
            self._telescope.sync_to(ra, dec)
            self.log_message.emit("CMD", f"Sync at RA {ra:.4f}h Dec {dec:+.4f}°")
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Sync: {exc}")

    def center_target_from_solution(
        self,
        solved_ra_h: float,
        solved_dec_d: float,
        target_ra_h: float,
        target_dec_d: float,
    ) -> None:
        """Correct the mount model from a solved field, then repeat the GoTo.

        This is intentionally observer-invoked: a background plate solve must
        never move the telescope.  ASTAP supplies the current field's J2000
        coordinates (RA hours, Dec degrees), which are synchronised before the
        requested target is slewed to again.
        """
        if not self._telescope:
            self.log_message.emit("WARN", "Center target: mount not connected")
            return
        try:
            self._telescope.set_tracking(True)
            self._telescope.sync_to(solved_ra_h, solved_dec_d)
            self.log_message.emit(
                "CMD",
                f"Center: synced solved field RA {solved_ra_h:.4f}h Dec {solved_dec_d:+.4f}°",
            )
            self._telescope.slew_to(target_ra_h, target_dec_d)
            self._target_radec = (target_ra_h, target_dec_d)
            self.slewed.emit()  # the pre-centering solution is now stale
            self.log_message.emit(
                "CMD", f"Center: slewing to target RA {target_ra_h:.4f}h Dec {target_dec_d:+.4f}°"
            )
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Center target: {exc}")

    def set_tracking(self, enabled: bool) -> None:
        if not self._telescope:
            return
        try:
            self._telescope.set_tracking(enabled)
            self.log_message.emit("CMD", f"Tracking {'ON' if enabled else 'OFF'}")
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Tracking: {exc}")

    def set_tracking_rate(self, idx: int) -> None:
        if not self._telescope:
            return
        names = ("Sidereal", "Lunar", "Solar")
        try:
            self._telescope.set_tracking_rate(idx)
            self.log_message.emit("CMD", f"Tracking rate → {names[idx]}")
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Tracking rate: {exc}")

    def abort_slew(self) -> None:
        if not self._telescope:
            return
        try:
            self._telescope.abort_slew()
            self.log_message.emit("CMD", "Slew aborted")
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Abort: {exc}")

    def park(self) -> None:
        if not self._telescope:
            return
        try:
            self._telescope.park()
            self.log_message.emit("CMD", "Park — arm closing.")
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Park: {exc}")

    def jog(self, axis: int, rate: float) -> None:
        """Start (rate ≠ 0) or stop (rate = 0) a MoveAxis jog, off-thread."""
        if not self._telescope:
            return
        # Off-thread: first MoveAxis on a fresh TCP connection takes ~600ms.
        # Calling it synchronously on the UI thread would freeze the UI and,
        # worse, consume the button-released event before the call returns —
        # resulting in an immediate stop and zero visible movement.
        QThreadPool.globalInstance().start(
            _JogRunnable(self._telescope, axis, rate, self.log_message)
        )

    # ------------------------------------------------------------------
    # Filter wheel commands
    # ------------------------------------------------------------------

    def move_filter(self, position: int) -> None:
        if not self._filterwheel:
            return
        self.device_state_changed.emit("filterwheel", "busy", "moving")
        QThreadPool.globalInstance().start(
            _FilterMoveRunnable(self._filterwheel, position, self._filter_settled, self.log_message)
        )

    @staticmethod
    def filter_position_for(name: str) -> int | None:
        """Wheel position whose slot name matches ``name`` (case-insensitive)."""
        slots = position_names()
        return next((i for i, n in slots.items() if str(n).lower() == name.lower()), None)

    @pyqtSlot(int, str)
    def _on_filter_settled(self, position: int, name: str) -> None:
        self.filter_moved.emit(position, name)
        self.device_state_changed.emit("filterwheel", "connected", name)
        self.log_message.emit("CMD", f"Filter → {name}")

    # ------------------------------------------------------------------
    # Focuser commands
    # ------------------------------------------------------------------

    def focuser_step(self, delta: int) -> int | None:
        """Relative move; returns the commanded target position, or ``None``."""
        if not self._focuser:
            return None
        try:
            target = self._focuser.step(delta)
            self.log_message.emit("CMD", f"Focuser step {delta:+d} → pos {target}")
            return target
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Focuser step: {exc}")
            return None

    def focuser_move_to(self, position: int) -> bool:
        if not self._focuser:
            return False
        try:
            self._focuser.move_to(position)
            self.log_message.emit("CMD", f"Focuser move → {position}")
            return True
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Focuser move: {exc}")
            return False

    def focuser_halt(self) -> None:
        if not self._focuser:
            return
        try:
            self._focuser.halt()
            self.log_message.emit("CMD", "Focuser halted")
        except AlpacaError as exc:
            self.log_message.emit("WARN", f"Focuser halt: {exc}")

    # ------------------------------------------------------------------
    # Camera parameters + reads
    # ------------------------------------------------------------------

    def set_camera_offset(self, value: int) -> None:
        if not self._camera:
            return
        try:
            self._camera.set_offset(value)
            self.log_message.emit("CMD", f"Offset → {value}")
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Offset: {exc}")

    def set_camera_binning(self, value: int) -> None:
        if not self._camera:
            return
        try:
            self._camera.set_binning(value)
            self.log_message.emit("CMD", f"Binning → {value}×{value}")
        except AlpacaError as exc:
            self.log_message.emit("ERROR", f"Binning: {exc}")

    def ccd_temperature(self) -> float | None:
        """Sensor temperature; ``None`` when unavailable (tolerant read)."""
        cam = self._camera
        if cam is None:
            return None
        try:
            return cam.get_ccd_temperature()
        except Exception:
            return None

    def egain_driver(self) -> float | None:
        """Driver-reported e-/ADU; ``None`` when unavailable (tolerant read)."""
        cam = self._camera
        if cam is None:
            return None
        try:
            return cam.get_electrons_per_adu()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Stellarium server
    # ------------------------------------------------------------------

    def start_stellarium(self, host: str, port: int) -> None:
        if self._stellarium is not None:
            self.stop_stellarium()
        worker = StellariumWorker(host=host, port=port)
        worker.target_received.connect(self.stellarium_target)
        worker.client_count_changed.connect(self.stellarium_clients)
        worker.server_started.connect(self._on_stellarium_started)
        worker.server_stopped.connect(self._on_stellarium_stopped)
        worker.error_occurred.connect(self._on_stellarium_error)

        self._config.set("stellarium.host", host)
        self._config.set("stellarium.port", port)

        self._stellarium = worker
        worker.start()
        self.log_message.emit("INFO", f"Stellarium server starting on {host}:{port}")

    def stop_stellarium(self) -> None:
        worker = self._stellarium
        if worker is None:
            return
        worker.stop()
        worker.wait(3000)
        self._stellarium = None

    def _on_stellarium_started(self) -> None:
        self.stellarium_state.emit(True)

    def _on_stellarium_stopped(self) -> None:
        self.stellarium_state.emit(False)

    def _on_stellarium_error(self, message: str) -> None:
        self.log_message.emit("ERROR", f"Stellarium: {message}")
        self.stellarium_error.emit(message)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop every session-owned thread. Call after the engine's shutdown
        (its workers hold device references) and before dropping the handles."""
        self._mount_retry.stop()
        self.stop_stellarium()
        self._stop_polling()
        self._stop_camera_temp_poll()
        self._stop_focuser_temp_poll()
        if self._discovery is not None and self._discovery.isRunning():
            self._discovery.wait(2000)
