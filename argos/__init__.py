"""Argos — desktop astrophotography and differential photometry for ZWO Seestar.

The single source of truth for the application version. Everything that shows
or stamps a version — the window title, the Settings page, the FITS
``SWCREATE``/``CREATOR`` headers, the splash screen — reads it from here, so a
release is a one-line change.
"""

__version__ = "0.4.2"

#: Software identity stamped into FITS headers by the writer.
SOFTWARE_ID = f"Argos v{__version__}"
