"""ASCOM Alpaca telescope wrapper — backed by alpyca.

Uses the official alpyca library (ASCOM Initiative) instead of raw HTTP
calls. Key benefits:
- Automatic ImageBytes support (8x faster image transfers)
- Typed exceptions with error codes
- Thread-safe (safe to call from QThread workers)

All methods are synchronous and must run inside a QThread worker.
Never call them from the Qt main thread.

Seestar S30 Pro known limitations:
  - SlewToAltAzAsync→ DriverException (error 1024) — not implemented
  - Unpark          → does not open the arm physically
  - MoveAxis        → confirmed working on firmware 7.18+ (tested 2026-05)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from alpaca.telescope import Telescope as _AlpacaTelescope
from alpaca.exceptions import DriverException

from argos.core.alpaca.client import AlpacaError

logger = logging.getLogger(__name__)


# ASCOM ``EquatorialCoordinateType`` values.  Keep these numeric rather than
# retaining the alpyca enum: a few drivers return a plain integer over Alpaca.
_EQU_OTHER = 0
_EQU_TOPOCENTRIC = 1
_EQU_J2000 = 2
_EQU_J2050 = 3
_EQU_B1950 = 4
_EQUATORIAL_SYSTEM_NAMES = {
    _EQU_OTHER: "other/unknown",
    _EQU_TOPOCENTRIC: "topocentric-of-date",
    _EQU_J2000: "J2000",
    _EQU_J2050: "J2050",
    _EQU_B1950: "B1950",
}


def _wrap(exc: Exception) -> AlpacaError:
    """Convert an alpyca exception into our internal AlpacaError."""
    if isinstance(exc, DriverException):
        return AlpacaError(exc.number, str(exc))
    return AlpacaError(0, str(exc))


@dataclass
class MountPosition:
    """Current pointing position of the mount."""

    ra: float  # Right Ascension in decimal hours (J2000)
    dec: float  # Declination in decimal degrees (J2000)
    altitude: float  # Altitude in degrees
    azimuth: float  # Azimuth in degrees
    tracking: bool  # Tracking enabled
    slewing: bool  # Slew in progress

    def ra_str(self) -> str:
        total_seconds = int(self.ra * 3600)
        h = total_seconds // 3600
        m = (total_seconds % 3600) // 60
        s = total_seconds % 60
        return f"{h:02d}h {m:02d}m {s:02d}s"

    def dec_str(self) -> str:
        sign = "+" if self.dec >= 0 else "-"
        d = abs(self.dec)
        deg = int(d)
        minutes = int((d - deg) * 60)
        seconds = int(((d - deg) * 60 - minutes) * 60)
        return f"{sign}{deg:02d}° {minutes:02d}' {seconds:02d}\""

    def alt_str(self) -> str:
        return f"{self.altitude:.2f}°"

    def az_str(self) -> str:
        return f"{self.azimuth:.2f}°"


class Telescope:
    """ASCOM Alpaca telescope controller, backed by alpyca.

    Args:
        host: IP address of the Seestar.
        port: Alpaca HTTP port (discovered via UDP, 32323 on the Seestar).
    """

    def __init__(self, host: str, port: int) -> None:
        self._scope = _AlpacaTelescope(f"{host}:{port}", 0)
        self._connected = False
        self._can_slew_async = True
        self._alignment_mode: str | None = None
        # All Argos/catalogue coordinates are ICRS/J2000.  This property says
        # what coordinate system the *driver* expects for both GoTo and Sync.
        # It is unrelated to AlignmentMode (Alt-Az vs EQ mechanics).
        self._equatorial_system: int | None = None
        self._site: tuple[float, float, float] | None = None
        # A mount can acknowledge UTCDate writes without actually changing its
        # clock.  In EQ mode that turns every otherwise-valid GoTo into a large
        # hour-angle error, so keep a read-back measurement for command safety.
        self._clock_error_seconds: float | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> str:
        """Connect to the mount and return its name.

        Sequence:
          1. Connected = True
          2. Sync UTC date (fixes Seestar clock-sync bug on firmware < 3.0.2)
          3. Check CanSlewAsync

        Raises:
            AlpacaError: Connection or device error.
        """
        try:
            self._scope.Connected = True
        except Exception as exc:
            raise _wrap(exc) from exc

        self._connected = True

        # Sync and *verify* the clock.  Some Seestar driver versions acknowledge
        # UTCDate writes but retain a stale local clock; an EQ GoTo would then
        # be aimed many hours away even with correct RA/Dec.
        try:
            utc_now = datetime.now(timezone.utc)
            try:
                before = self._scope.UTCDate
                logger.info("Mount UTC before sync: %s", before)
            except Exception:
                logger.debug("UTCDate read failed — writing first (some firmware requires this)")
            # Pass an aware datetime, which alpyca serialises as ISO-8601.  It
            # is less ambiguous than hand-assembling a string for drivers with
            # fragile date parsers.
            self._scope.UTCDate = utc_now
            mount_utc = self._scope.UTCDate
            if mount_utc.tzinfo is None:
                mount_utc = mount_utc.replace(tzinfo=timezone.utc)
            self._clock_error_seconds = abs((mount_utc - utc_now).total_seconds())
            if self._clock_error_seconds > 60.0:
                logger.error(
                    "Mount UTC remains %.0fs off after sync (mount=%s system=%s); "
                    "GoTo and Sync will be refused",
                    self._clock_error_seconds,
                    mount_utc,
                    utc_now,
                )
            else:
                logger.info(
                    "Mount UTC verified: %s (diff=%.1fs)", mount_utc, self._clock_error_seconds
                )
        except Exception as exc:
            logger.warning("UTC date sync failed (non-fatal): %s", exc)

        try:
            self._can_slew_async = bool(self._scope.CanSlewAsync)
            if not self._can_slew_async:
                logger.warning("Mount reports CanSlewAsync=False — will poll Slewing property")
        except Exception:
            self._can_slew_async = True

        # ASCOM requires SlewToCoordinates[Async] parameters to be expressed
        # in EquatorialSystem.  Most mounts report equTopocentric, whereas the
        # catalogue, ASTAP and Stellarium interfaces used by Argos are J2000.
        # Reading this is therefore essential to a correct GoTo, independently
        # of the mount's physical AlignmentMode.
        try:
            value = int(self._scope.EquatorialSystem)
            if value in _EQUATORIAL_SYSTEM_NAMES:
                self._equatorial_system = value
                logger.info(
                    "EquatorialSystem: %s (%s)",
                    value,
                    _EQUATORIAL_SYSTEM_NAMES[value],
                )
            else:
                logger.warning("Unknown EquatorialSystem=%s; retaining J2000 coordinates", value)
        except Exception as exc:
            logger.warning("EquatorialSystem query failed; retaining J2000 coordinates: %s", exc)

        # A site is only a small correction for stellar targets, but reading it
        # lets the topocentric conversion match the driver's contract exactly.
        # It is optional because several Seestar firmware versions do not
        # expose the Site* properties through Alpaca.
        if self._equatorial_system == _EQU_TOPOCENTRIC:
            try:
                latitude = float(self._scope.SiteLatitude)
                longitude = float(self._scope.SiteLongitude)
                if -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0:
                    try:
                        elevation = float(self._scope.SiteElevation)
                    except Exception:
                        elevation = 0.0
                    self._site = (latitude, longitude, elevation)
                    logger.info(
                        "Mount site: lat=%.5f lon=%.5f elev=%.0fm",
                        latitude,
                        longitude,
                        elevation,
                    )
            except Exception as exc:
                logger.info(
                    "Mount site unavailable; using geocentric apparent coordinates: %s", exc
                )

        try:
            from alpaca.telescope import TelescopeAxes

            can_primary = self._scope.CanMoveAxis(TelescopeAxes.axisPrimary)
            can_secondary = self._scope.CanMoveAxis(TelescopeAxes.axisSecondary)
            logger.info("CanMoveAxis Primary=%s Secondary=%s", can_primary, can_secondary)
        except Exception as exc:
            logger.debug("CanMoveAxis query failed (non-fatal): %s", exc)

        # Mount geometry — the Seestar runs either alt-az (native) or EQ
        # (on a wedge, switched in the Seestar app). Field rotation only
        # exists in alt-az, so surface which one the firmware reports.
        try:
            from alpaca.telescope import AlignmentModes

            mode = self._scope.AlignmentMode
            self._alignment_mode = {
                AlignmentModes.algAltAz: "Alt-Az",
                AlignmentModes.algPolar: "EQ",
                AlignmentModes.algGermanPolar: "EQ (GEM)",
            }.get(mode)
            logger.info("AlignmentMode: %s → %s", mode, self._alignment_mode)
        except Exception as exc:
            self._alignment_mode = None
            logger.debug("AlignmentMode query failed (non-fatal): %s", exc)

        try:
            name = self._scope.Name or "Unknown"
        except Exception:
            name = "Unknown"

        logger.info("Telescope connected: %s (CanSlewAsync=%s)", name, self._can_slew_async)
        return name

    def disconnect(self) -> None:
        """Disconnect from the mount."""
        try:
            self._scope.Connected = False
        except Exception as exc:
            logger.warning("Error during disconnect: %s", exc)
        finally:
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def alignment_mode(self) -> str | None:
        """Mount geometry reported at connect: ``"Alt-Az"``, ``"EQ"``,
        ``"EQ (GEM)"``, or None when the firmware doesn't say."""
        return self._alignment_mode

    @property
    def equatorial_system(self) -> str | None:
        """Coordinate system currently required by the mount driver.

        This is deliberately separate from :attr:`alignment_mode`: an EQ
        mount can still require topocentric-of-date coordinates.
        """
        if self._equatorial_system is None:
            return None
        return _EQUATORIAL_SYSTEM_NAMES[self._equatorial_system]

    @property
    def clock_error_seconds(self) -> float | None:
        """Absolute mount-vs-system UTC offset measured immediately after sync."""
        return self._clock_error_seconds

    def _assert_clock_synchronized(self) -> None:
        """Refuse coordinate commands when the mount clock is demonstrably stale."""
        if self._clock_error_seconds is not None and self._clock_error_seconds > 60.0:
            raise AlpacaError(
                0,
                "Mount UTC is %.1f hours off. Correct the Seestar date/time in its native app, "
                "then reconnect before using GoTo or Sync." % (self._clock_error_seconds / 3600.0),
            )

    # ------------------------------------------------------------------
    # Coordinate-system boundary
    # ------------------------------------------------------------------

    def _convert_coordinates(
        self, ra: float, dec: float, direction: Literal["to_mount", "from_mount"]
    ) -> tuple[float, float]:
        """Convert between Argos ICRS/J2000 and the driver's coordinates.

        ASCOM's ``equTopocentric`` means apparent coordinates at the current
        date (precession, nutation and annual aberration included), with right
        ascension counted from the true equinox of date.  That is astropy's
        ``TETE`` frame, not ``CIRS``: CIRS counts right ascension from the
        CIO, which does not follow precession along the equator, so it leaves
        out the term that dominates.  At the equator that term is about 21
        arcminutes in 2026 and grows by 0.77 arcminutes a year.

        The transformation is intentionally at this wrapper boundary, so the
        rest of Argos — catalogue resolution, ASTAP, FITS and Stellarium —
        continues to exchange its documented J2000 coordinates.
        """
        if self._equatorial_system in (None, _EQU_OTHER, _EQU_J2000):
            return ra, dec

        try:
            import astropy.units as u
            from astropy.coordinates import FK4, FK5, TETE, EarthLocation, SkyCoord
            from astropy.time import Time
            from astropy.utils import iers

            # Telescope control must remain offline-safe.  Astropy otherwise
            # attempts an IERS download during a live GoTo and may reject a
            # perfectly usable bundled table once it ages past 30 days.
            iers.conf.auto_download = False
            iers.conf.auto_max_age = None

            if self._equatorial_system == _EQU_TOPOCENTRIC:
                now = Time(datetime.now(timezone.utc))
                frame_args = {"obstime": now}
                if self._site is not None:
                    latitude, longitude, elevation = self._site
                    frame_args["location"] = EarthLocation.from_geodetic(
                        longitude * u.deg, latitude * u.deg, elevation * u.m
                    )
                frame = TETE(**frame_args)
            elif self._equatorial_system == _EQU_J2050:
                frame = FK5(equinox=Time("J2050"))
            elif self._equatorial_system == _EQU_B1950:
                frame = FK4(equinox=Time("B1950"))
            else:
                # A future ASCOM value must never silently become a physical
                # mispoint.  This only runs if a value passed connection-time
                # validation, but keeps the safety property local to the
                # conversion boundary.
                raise ValueError(f"unsupported EquatorialSystem={self._equatorial_system}")

            if direction == "to_mount":
                coord = SkyCoord(ra=ra * 15.0 * u.deg, dec=dec * u.deg, frame="icrs")
                converted = coord.transform_to(frame)
            else:
                coord = SkyCoord(ra=ra * 15.0 * u.deg, dec=dec * u.deg, frame=frame)
                converted = coord.icrs
            return float(converted.ra.hour % 24.0), float(converted.dec.deg)
        except Exception as exc:
            raise AlpacaError(
                0,
                f"Could not convert {self.equatorial_system} mount coordinates: {exc}",
            ) from exc

    def _coordinates_for_mount(self, ra: float, dec: float) -> tuple[float, float]:
        """Return J2000 Argos coordinates in the driver's required system."""
        return self._convert_coordinates(ra, dec, "to_mount")

    def _coordinates_from_mount(self, ra: float, dec: float) -> tuple[float, float]:
        """Return driver-reported coordinates as Argos ICRS/J2000."""
        return self._convert_coordinates(ra, dec, "from_mount")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_position(self) -> MountPosition:
        """Read the current position from the mount.

        Raises:
            AlpacaError: Communication or device error.
        """
        try:
            mount_ra = float(self._scope.RightAscension)
            mount_dec = float(self._scope.Declination)
            ra, dec = self._coordinates_from_mount(mount_ra, mount_dec)
            return MountPosition(
                ra=ra,
                dec=dec,
                altitude=float(self._scope.Altitude),
                azimuth=float(self._scope.Azimuth),
                tracking=bool(self._scope.Tracking),
                slewing=bool(self._scope.Slewing),
            )
        except Exception as exc:
            raise _wrap(exc) from exc

    def get_altaz(self) -> tuple[float, float]:
        """Return current (altitude, azimuth) in degrees."""
        try:
            return float(self._scope.Altitude), float(self._scope.Azimuth)
        except Exception as exc:
            raise _wrap(exc) from exc

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def slew_to(self, ra: float, dec: float) -> None:
        """Start an asynchronous GoTo slew (mirrors NINA sequence).

        1. Check AtPark — raise if parked
        2. Enable tracking
        3. Set TargetRightAscension / TargetDeclination (log failures, don't hide them)
        4. Call SlewToCoordinatesAsync
        5. Verify Slewing=True was set (warn if not — some mounts report False when
           already at target, which is acceptable)

        Args:
            ra:  Target RA in decimal hours (J2000), 0–24.
            dec: Target Dec in decimal degrees (J2000), -90 to +90.

        Raises:
            AlpacaError: Mount parked or slew rejected.
        """
        import time

        try:
            self._assert_clock_synchronized()
            if self._scope.AtPark:
                raise AlpacaError(0, "Mount is parked — unpark before slewing")

            # Tracking must be enabled BEFORE setting targets
            logger.info("Enabling tracking")
            self._scope.Tracking = True

            mount_ra, mount_dec = self._coordinates_for_mount(ra, dec)

            # Set target coordinates — log if unsupported, but continue
            try:
                self._scope.TargetRightAscension = mount_ra
                self._scope.TargetDeclination = mount_dec
                logger.info(
                    "Target set (%s): RA=%.6f Dec=%.6f",
                    self.equatorial_system or "J2000 assumed",
                    mount_ra,
                    mount_dec,
                )
            except Exception as exc:
                logger.warning("TargetRA/Dec not writable on this firmware: %s", exc)

            logger.info(
                "SlewToCoordinatesAsync (%s) RA=%.6f Dec=%.6f",
                self.equatorial_system or "J2000 assumed",
                mount_ra,
                mount_dec,
            )
            self._scope.SlewToCoordinatesAsync(mount_ra, mount_dec)

            # Brief delay then verify slew started
            time.sleep(0.3)
            if not self._scope.Slewing:
                logger.warning(
                    "Mount did not report Slewing=True — already at target or slew rejected"
                )

            # For synchronous mounts (CanSlewAsync=False): poll until slew completes (NINA pattern)
            if not self._can_slew_async:
                deadline = time.time() + 300.0
                while self._scope.Slewing and time.time() < deadline:
                    time.sleep(1.0)
                if time.time() >= deadline:
                    logger.error("Slew timeout after 300s")
                else:
                    logger.info("Slew complete (sync-mount polling)")

        except AlpacaError:
            raise
        except Exception as exc:
            raise _wrap(exc) from exc

    def set_tracking(self, enabled: bool) -> None:
        """Enable or disable sidereal tracking."""
        try:
            self._scope.Tracking = enabled
            logger.info("Tracking: %s", enabled)
        except Exception as exc:
            raise _wrap(exc) from exc

    def abort_slew(self) -> None:
        """Immediately stop any ongoing slew."""
        try:
            self._scope.AbortSlew()
            logger.info("Slew aborted")
        except Exception as exc:
            raise _wrap(exc) from exc

    def is_parked(self) -> bool:
        """Return True if the mount reports being parked."""
        try:
            return bool(self._scope.AtPark)
        except Exception as exc:
            raise _wrap(exc) from exc

    def park(self) -> None:
        """Park the mount (closes the mechanical arm on Seestar S30 Pro).

        If atpark=True (e.g. native app opened arm without Alpaca Unpark),
        sends Unpark first so the subsequent Park triggers physical movement.

        Raises:
            AlpacaError: Mount does not support Park, or driver error.
        """
        try:
            if not self._scope.CanPark:
                raise AlpacaError(0, "Mount does not support Park")

            if self._scope.AtPark:
                logger.info("Mount reports already parked — sending Unpark first")
                self._scope.Unpark()

            logger.info("Park command sent")
            self._scope.Park()
        except AlpacaError:
            raise
        except Exception as exc:
            raise _wrap(exc) from exc

    def unpark(self) -> None:
        """Unpark via Alpaca.

        Note: does not open the arm physically on the Seestar S30 Pro.
        """
        try:
            self._scope.Unpark()
            logger.info("Unpark sent")
        except Exception as exc:
            raise _wrap(exc) from exc

    def sync_to(self, ra: float, dec: float) -> None:
        """Sync the mount's pointing model to the given coordinates.

        Used after plate solving to correct the pointing model.

        Raises:
            AlpacaError: Tracking not enabled, or sync rejected by mount.
        """
        try:
            self._assert_clock_synchronized()
            if not self._scope.Tracking:
                raise AlpacaError(
                    0, "Tracking must be enabled before syncing — enable tracking first"
                )
            mount_ra, mount_dec = self._coordinates_for_mount(ra, dec)
            logger.info(
                "Syncing (%s) to RA=%.6f Dec=%.6f", self.equatorial_system, mount_ra, mount_dec
            )
            self._scope.SyncToCoordinates(mount_ra, mount_dec)
        except AlpacaError:
            raise
        except Exception as exc:
            raise _wrap(exc) from exc

    def move_axis(self, axis: int, rate: float) -> None:
        """Start continuous jog on one axis at the given rate (deg/s).

        The mount keeps moving until ``stop_axis()`` is called.

        Args:
            axis: 0 = Primary (RA/Az), 1 = Secondary (Dec/Alt).
            rate: Speed in deg/s. Positive = North/East, negative = South/West.
                  Pass 0.0 to stop.

        Raises:
            AlpacaError: Device error or axis not supported.
        """
        try:
            from alpaca.telescope import TelescopeAxes

            ax = TelescopeAxes.axisPrimary if axis == 0 else TelescopeAxes.axisSecondary
            self._scope.MoveAxis(ax, rate)
            logger.debug("MoveAxis axis=%d rate=%.3f", axis, rate)
        except Exception as exc:
            raise _wrap(exc) from exc

    def stop_axis(self, axis: int) -> None:
        """Stop a jogging axis (sets rate to 0).

        Args:
            axis: 0 = Primary (RA/Az), 1 = Secondary (Dec/Alt).
        """
        self.move_axis(axis, 0.0)

    def pulse_guide(self, direction: int, duration_ms: int) -> None:
        """Send a PulseGuide command (guide-rate only — not for manual jogging).

        Args:
            direction: 0=North, 1=South, 2=East, 3=West (ASCOM constants).
            duration_ms: Duration in milliseconds.
        """
        try:
            self._scope.PulseGuide(direction, duration_ms)
        except Exception as exc:
            raise _wrap(exc) from exc

    # ------------------------------------------------------------------
    # Tracking rate, target hint, side of pier
    # ------------------------------------------------------------------

    def set_tracking_rate(self, rate: int) -> None:
        """Select the tracking rate model.

        Args:
            rate: ASCOM ``DriveRates`` enum value:
                  0=Sidereal, 1=Lunar, 2=Solar, 3=King.

        Raises:
            AlpacaError: Mount rejected the rate (e.g. only Sidereal supported).
        """
        try:
            from alpaca.telescope import DriveRates

            self._scope.TrackingRate = DriveRates(rate)
            logger.info("Tracking rate set to %s", DriveRates(rate).name)
        except Exception as exc:
            raise _wrap(exc) from exc

    def get_tracking_rate(self) -> int | None:
        """Return the current ASCOM ``DriveRates`` value, or None if unsupported."""
        try:
            return int(self._scope.TrackingRate)
        except Exception as exc:
            logger.debug("TrackingRate read failed: %s", exc)
            return None

    def set_target(self, ra: float, dec: float) -> None:
        """Set the mount's TargetRightAscension/TargetDeclination without slewing.

        Useful before a slew or sync so the mount's pointing model records what
        we were aiming at — independent of the actual pointing it eventually
        achieves. Failures are logged and re-raised; some firmwares do not allow
        writing these properties (in which case the slew code path tolerates
        the failure on its own).
        """
        try:
            self._assert_clock_synchronized()
            mount_ra, mount_dec = self._coordinates_for_mount(ra, dec)
            self._scope.TargetRightAscension = mount_ra
            self._scope.TargetDeclination = mount_dec
            logger.debug(
                "Target hint set (%s): RA=%.6f Dec=%.6f",
                self.equatorial_system,
                mount_ra,
                mount_dec,
            )
        except Exception as exc:
            raise _wrap(exc) from exc

    def side_of_pier(self) -> str | None:
        """Return 'EAST' / 'WEST' / 'UNKNOWN', or None if the mount can't tell.

        For an alt-az like the Seestar this concept is meaningless — the
        property may simply not be exposed.
        """
        try:
            value = int(self._scope.SideOfPier)
        except Exception:
            return None
        # ASCOM PierSide: -1 unknown, 0 east, 1 west
        return {0: "EAST", 1: "WEST"}.get(value, "UNKNOWN")
