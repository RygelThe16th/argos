"""Persistent application configuration stored as JSON.

Config file location: ~/.argos/config.json
All values have sensible defaults and can be updated at runtime.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path.home() / ".argos"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

_DEFAULTS: dict[str, Any] = {
    "alpaca": {
        "host": "",
        "port": 32323,  # Alpaca HTTP port (4700 is the native JSON-RPC port)
    },
    "sessions_path": str(Path.home() / "Argos" / "sessions"),
    # User-visible locations for durable observing data and replaceable
    # catalogue caches.  The bundled essential catalogue is read-only inside
    # the application; these are only the caches acquired on demand.
    "data_paths": {
        # A session is Argos's working folder: each observation gets its own
        # subfolder holding FITS frames, metadata, preview products and logs.
        "sequence_presets_directory": str(Path.home() / "Argos" / "sequences"),
        # Per-sensor photon-transfer calibrations are deliberately separate
        # from sessions, because they remain valid across observing runs.
        "camera_calibration_directory": str(Path.home() / ".argos"),
        "object_catalogue_cache": str(Path.home() / "Argos" / "cache" / "object_resolver.json"),
        "exoplanet_cache": str(Path.home() / "Argos" / "cache" / "exoplanets.json"),
        "aavso_cache_directory": str(Path.home() / ".argos" / "cache" / "catalog"),
        "gaia_cache_directory": str(Path.home() / ".argos" / "cache" / "gaia"),
        "field_catalogue_cache_directory": str(
            Path.home() / ".argos" / "cache" / "field_catalogue"
        ),
    },
    "observer": {
        "name": "",
        "obscode": "",  # AAVSO observer code — stamped on every AAVSO export
        "latitude": 0.0,
        "longitude": 0.0,
        "elevation": 0.0,
    },
    "site": {
        "name": "",
        "latitude": 0.0,
        "longitude": 0.0,
        "elevation": 0.0,
        "favorites": [],
    },
    "ui": {
        "log_level": "INFO",
        "window_state": None,  # base64 QMainWindow.saveState()
        "window_geometry": None,
    },
    # Which telescope Argos is driving. "profile" names an entry in
    # argos.core.hardware.catalog; "overrides" carries the few values an
    # observer may correct for their own unit (see hardware.active.OVERRIDABLE).
    # Optics and CFA layout are deliberately not overridable — if those are
    # wrong the profile is wrong, and it needs fixing at the source.
    "hardware": {
        "profile": "s30pro",
        "overrides": {},
    },
    # Sensor characteristics — confirm against hardware (see notes/capture_panel.md §8).
    # IMX585 Starvis 2: 12-bit ADC scaled to 16-bit; "full_well_adu" is the
    # saturation/linearity threshold used by the display clipping indicator.
    #
    # DEPRECATED: adc_bits / full_well_adu / linearity_max_adu now live on the
    # telescope profile. They are migrated into hardware.overrides on first
    # load and kept here only so an older Argos can still read the file.
    "camera": {
        "adc_bits": 12,
        "full_well_adu": 60000,
        "linearity_max_adu": 50000,
        "egain_table": {},  # {gain_value: e-/ADU}; empty → driver/sensor reference lookup
    },
    # Plate-solving (ASTAP) + the live auto-solve policy. See
    # notes/photometry_plan.md §4/§8. Empty astap_path/database → auto-detect.
    "astrometry": {
        "astap_path": "",
        "database": "",
        "database_path": "",  # optional ASTAP star-database directory (-d)
        "downsample": 2,
        "search_radius_deg": 30,  # thorough/manual solve (around a hint)
        "use_scale_hint": True,
        "grid_spacing_arcmin": 0,  # 0 = adaptive 1/2/5 grid
        "live_search_radius_deg": 5,  # small radius w/ mount hint (auto-solve)
        "live_resolve_s": 20,  # re-solve cadence ceiling (auto)
        "live_resolve_arcmin": 2,  # re-solve once the mount moves this far
        "live_timeout_s": 25,  # bound the live solve so the cadence never stalls
    },
    # VSX/VSP variable-star catalog cone search (notes/photometry_plan.md §5).
    "catalog": {
        "mag_limit": 15.0,
        "max_results": 250,
        "include_suspected": True,
        # Named sources for the solved field; this is visual identification,
        # not a calibrated comparison-star catalogue.
        "field_stars_enabled": True,
        # ``Field`` is the useful default on a modern wide-field camera.  A
        # shallow list makes a solved image look as though most stars are
        # unknown; observers can switch to Quick/Deep in Field catalogue.
        # Cache a deeper Gaia field once, then let the live image slider filter
        # it locally without a network round-trip on every movement.
        "field_stars_mag_limit": 18.0,
        "field_stars_max_results": 2000,
        # Total online identity budget shared between Gaia and SIMBAD. VSX,
        # VSP and the tiny NASA result remain independent science catalogues.
        "identification_max_objects": 400,
        "display_mag_limit": 18.0,
        "display_limit_version": 2,
        # WCS is the source of truth for an identification overlay.  The local
        # peak detector is intentionally optional: it can miss faint or
        # colour-dependent sources in a raw CFA preview.
        "field_stars_detected_only": False,
        # These are local/cache-backed context layers.  They never initiate a
        # network request during a capture.
        "show_essential_objects": True,
        "show_cached_exoplanets": True,
        # Every explicit Field → Identify field operation can enrich the
        # solved field from these authoritative services.  Disable either
        # control for cache-only/offline observing.
        "named_objects_enabled": True,
        "named_objects_max_results": 500,
        "named_objects_allow_network": True,
        "exoplanet_hosts_enabled": True,
        "exoplanet_hosts_allow_network": True,
    },
    # Differential-photometry preview (notes/photometry_plan.md §6).
    "photometry": {
        "aperture_fwhm_mult": 2.5,
        "aperture_min_px": 4,
        "annulus_in_px": 8,
        "annulus_out_px": 12,
        "read_noise_e": 1.5,
        "default_band": "TG",
        "min_comparisons": 2,
        "auto_comparisons": 5,  # comps auto-picked when a target is chosen
        # Pilot-frame quality gate for automatic VSP comparison selection.
        # Proximity alone is insufficient in a wide Seestar field: faint
        # stars close to the target can have unusable differential errors.
        "comparison_min_snr": 10.0,
        "comparison_max_delta_mag": 1.5,
        # References across a very wide raw field are vulnerable to flat-field
        # residuals and differential tracking error. A smaller, validated
        # ensemble is preferable to filling a requested count with remote stars.
        "comparison_max_separation_arcmin": 25.0,
        # Reference stars this close (green px) to a frame edge, beyond the
        # background annulus, are not proposed: unguided tracking drifts the
        # field by roughly 90 green px per hour on a Seestar S30 Pro.
        "comparison_edge_margin_px": 100.0,
        "comparison_validation_min_points": 10,
        "comparison_validation_max_scatter_mag": 0.10,
        "comparison_validation_max_formal_error_mag": 0.10,
        # ``star_var_script``-compatible run-level error floor. ``None``
        # derives it from the light curve after ten valid points; a numeric
        # value is an observer-approved systematic floor in magnitudes.
        "systematic_floor_mag": None,
    },
    "diagnostics": {
        # Opt-in local JSONL flight recorder inside each session. This is never
        # sent anywhere; observers explicitly choose whether to create it.
        "enabled": False,
    },
    "stellarium": {
        "host": "127.0.0.1",
        "port": 10001,
        # A Stellarium goto contains a sky coordinate.  Never send that
        # coordinate to a remote catalogue without an explicit opt-in; local
        # cached target matches remain available regardless of this setting.
        "online_target_lookup": False,
    },
}


class Config:
    """Application configuration backed by a JSON file.

    Usage:
        config = Config.load()
        config.set("alpaca.host", "192.168.1.42")
        config.save()
        host = config.get("alpaca.host")
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> "Config":
        """Load config from disk, creating defaults if the file does not exist."""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not _CONFIG_FILE.exists():
            logger.info("No config file found, creating defaults at %s", _CONFIG_FILE)
            instance = cls(_deep_copy(_DEFAULTS))
            instance.save()
            return instance

        try:
            with _CONFIG_FILE.open("r", encoding="utf-8") as f:
                on_disk = json.load(f)
            data = _deep_merge(_DEFAULTS, on_disk)
            _migrate_camera_keys(data)
            _migrate_legacy_site(data, on_disk)
            _migrate_alpaca_profiles(data, on_disk)
            _migrate_diagnostics_opt_in(data, on_disk)
            _migrate_catalogue_display_limit(data, on_disk)
            logger.debug("Config loaded from %s", _CONFIG_FILE)
            return cls(data)
        except Exception as exc:
            logger.error("Failed to load config (%s), using defaults", exc)
            return cls(_deep_copy(_DEFAULTS))

    def save(self) -> None:
        """Persist current config to disk."""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with _CONFIG_FILE.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            logger.debug("Config saved to %s", _CONFIG_FILE)
        except Exception as exc:
            logger.error("Failed to save config: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value using dot-notation key (e.g. 'alpaca.host')."""
        parts = key.split(".")
        node: Any = self._data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        """Set a value using dot-notation key and persist immediately."""
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self.save()

    # Convenience properties for the most-used values

    @property
    def alpaca_host(self) -> str:
        return self.get("alpaca.host", "")

    @alpaca_host.setter
    def alpaca_host(self, value: str) -> None:
        self.set("alpaca.host", value)

    @property
    def alpaca_port(self) -> int:
        return self.get("alpaca.port", 32323)

    @alpaca_port.setter
    def alpaca_port(self, value: int) -> None:
        self.set("alpaca.port", value)

    @property
    def sessions_path(self) -> Path:
        return Path(self.get("sessions_path", str(Path.home() / "Argos" / "sessions")))

    @sessions_path.setter
    def sessions_path(self, value: Path | str) -> None:
        self.set("sessions_path", str(value))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _deep_copy(d: dict) -> dict:
    return json.loads(json.dumps(d))


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively. Override wins on conflicts."""
    result = _deep_copy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


#: Legacy ``camera.*`` keys that moved onto the telescope profile.
_MIGRATED_CAMERA_KEYS = ("adc_bits", "full_well_adu", "linearity_max_adu")


def _migrate_camera_keys(data: dict) -> None:
    """Carry hand-tuned ``camera.*`` values over to ``hardware.overrides``.

    Only values that actually differ from the shipped default are moved — an
    untouched config should not acquire overrides — and only when no overrides
    exist yet, so this runs once and never fights a later edit. The legacy keys
    are left in place: an older Argos reading the same file still works.

    Mutates *data* in place.
    """
    hardware = data.setdefault("hardware", {})
    existing = hardware.setdefault("overrides", {})
    if existing:
        return  # already migrated, or the user set their own

    camera = data.get("camera") or {}
    defaults = _DEFAULTS["camera"]
    moved = {
        key: camera[key]
        for key in _MIGRATED_CAMERA_KEYS
        if key in camera and camera[key] != defaults[key]
    }
    if moved:
        hardware["overrides"] = moved
        logger.info("Migrated tuned camera settings to hardware.overrides: %s", sorted(moved))


def _migrate_legacy_site(data: dict, on_disk: dict) -> None:
    """Promote pre-0.4 site coordinates nested under ``observer`` once.

    Earlier defaults exposed these values under ``observer`` although the
    capture pipeline reads ``site.*``.  Never overwrite an explicit modern
    site block, even when its values are zero (zero is a valid coordinate).
    """
    if "site" in on_disk:
        return
    observer = data.get("observer") or {}
    values = {key: observer.get(key) for key in ("latitude", "longitude", "elevation")}
    if any(value not in (None, 0, 0.0) for value in values.values()):
        site = data.setdefault("site", {})
        site.update(values)
        logger.info("Migrated legacy observer coordinates to site settings")


def _migrate_catalogue_display_limit(data: dict, on_disk: dict) -> None:
    """Adopt the G=18 field-identification default once on upgrade.

    Earlier builds persisted a shallow 14–15.5 display value, which made a
    dense solved field look mostly unidentified even after its deeper Gaia
    catalogue had been downloaded.  A version marker preserves every later
    observer adjustment.
    """
    saved = on_disk.get("catalog") or {}
    if int(saved.get("display_limit_version", 0) or 0) >= 2:
        return
    catalog = data.setdefault("catalog", {})
    catalog["display_mag_limit"] = 18.0
    catalog["display_limit_version"] = 2
    logger.info("Migrated solved-field Gaia display limit to G=18")


def _migrate_diagnostics_opt_in(data: dict, on_disk: dict) -> None:
    """Make pre-privacy-policy diagnostic recording opt-in on upgrade.

    Older releases created ``diagnostics.enabled: true`` automatically, so a
    true value in such a file is not evidence of an observer's affirmative
    choice.  Preserve only settings made after the local-only policy landed.
    """
    saved = on_disk.get("diagnostics") or {}
    if saved.get("local_opt_in_v1") is True:
        return
    diagnostics = data.setdefault("diagnostics", {})
    diagnostics["enabled"] = False
    logger.info("Local diagnostics disabled until explicitly enabled in Settings")


def _migrate_alpaca_profiles(data: dict, on_disk: dict) -> None:
    """Flatten pre-0.4.1 network profiles to one active IP/port endpoint."""
    old = on_disk.get("alpaca") or {}
    profiles = old.get("profiles")
    if not isinstance(profiles, dict):
        return
    selected = profiles.get(old.get("profile", "home"), {})
    if not isinstance(selected, dict):
        return
    alpaca = data.setdefault("alpaca", {})
    # An explicitly typed modern endpoint always wins over profile data.
    if not old.get("host") and selected.get("host"):
        alpaca["host"] = str(selected["host"])
    # ``port`` was always populated by the old defaults, so the selected
    # profile is the only reliable source for a non-standard legacy port.
    if selected.get("port"):
        alpaca["port"] = int(selected["port"])
    alpaca.pop("profile", None)
    alpaca.pop("profiles", None)
    logger.info("Migrated Alpaca network profiles to one IP/port endpoint")
