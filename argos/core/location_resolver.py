"""Resolve an observing site name to WGS84 coordinates and terrain elevation.

The public Nominatim endpoint is queried only after an explicit user action;
there is deliberately no keystroke autocomplete.  Elevation is a convenience
estimate from Open-Meteo's 90 m DEM and must be edited when an observatory has
a surveyed elevation.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from argos import __version__

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
_HEADERS = {"User-Agent": f"Argos/{__version__} (astronomical observing-site search)"}


class LocationResolutionError(RuntimeError):
    """A location name could not be resolved into usable coordinates."""


@dataclass(frozen=True)
class LocationResult:
    """One user-selectable observing site in WGS84 coordinates."""

    label: str
    latitude: float
    longitude: float
    elevation_m: float | None


def search_locations(query: str, *, timeout_s: float = 8.0) -> list[LocationResult]:
    """Return up to five geocoded places for a deliberate free-form search."""
    query = " ".join(query.split())
    if not query:
        raise LocationResolutionError("Enter a city, country, or address first.")
    try:
        response = requests.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 0},
            headers=_HEADERS,
            timeout=timeout_s,
        )
        response.raise_for_status()
        matches = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise LocationResolutionError(
            "Location search is unavailable. Check your internet connection."
        ) from exc
    if not isinstance(matches, list) or not matches:
        raise LocationResolutionError(f"No place found for ‘{query}’.")

    results: list[LocationResult] = []
    for match in matches:
        try:
            latitude = float(match["lat"])
            longitude = float(match["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            continue
        label = str(match.get("display_name") or query)
        results.append(
            LocationResult(label, latitude, longitude, _elevation(latitude, longitude, timeout_s))
        )
    if not results:
        raise LocationResolutionError(f"No usable coordinates found for ‘{query}’.")
    return results


def _elevation(latitude: float, longitude: float, timeout_s: float) -> float | None:
    """Get a terrain elevation without rejecting an otherwise useful place."""
    try:
        response = requests.get(
            _ELEVATION_URL,
            params={"latitude": latitude, "longitude": longitude},
            headers=_HEADERS,
            timeout=timeout_s,
        )
        response.raise_for_status()
        values = response.json().get("elevation")
        value = values[0] if isinstance(values, list) and values else None
        return float(value) if value is not None else None
    except (requests.RequestException, ValueError, TypeError, IndexError):
        return None
