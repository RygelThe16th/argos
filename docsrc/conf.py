"""Sphinx configuration for Argos documentation.

The Sphinx sources live in ``docsrc/``; ``docs/`` is the published website
(GitHub Pages serves it from the ``main`` branch) and is not part of this build.

Build with::

    cd docsrc
    sphinx-build -M html . _build

Or from the project root::

    uv run sphinx-build -b html docsrc docsrc/_build
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

# -- Project info -----------------------------------------------------------

project = "ARGOS"
author = "J. Perret"
copyright = f"{datetime.now().year}, {author}"
release = "0.4.2"

# -- General configuration -------------------------------------------------

extensions = [
    "myst_parser",          # MyST Markdown (.md) support
    "sphinx.ext.autodoc",   # Auto-generate API docs from docstrings
    "sphinx.ext.napoleon",  # Google/NumPy-style docstring support
    "sphinx.ext.viewcode",  # Link to source code
    "sphinx.ext.graphviz",  # DOT graph / diagram support
    "sphinx.ext.intersphinx",  # Cross-ref to Python, numpy, etc.
    "sphinx_design",        # Grids, cards, tabs, dropdowns
    "sphinx_copybutton",    # Copy button on code blocks
]

# MyST config — parse all .md files as MyST
myst_heading_anchors = 4          # auto-generate anchors down to h4
myst_enable_extensions = [
    "colon_fence",                 # ```{note} / ```{warning} directives
    "deflist",                     # definition lists
    "fieldlist",                   # field lists
    "html_image",                  # inline HTML images
    "dollarmath",                  # $inline$ and $$display$$ maths
    "amsmath",                     # align/aligned environments
    "tasklist",                    # - [ ] checkboxes
]

# autodoc — scan argos packages
sys.path.insert(0, os.path.abspath(".."))

# Custom mock for PyQt6 so pyqtSignal() doesn't produce *args signatures.
import unittest.mock as _mock

class _PyqtSignalInstance:
    """A pyqtSignal instance — Sphinx sees it as a plain object."""
    pass

class _PyqtSignal:
    """Mock for pyqtSignal — returns a plain instance."""
    def __new__(cls, *types, **kwargs):
        return _PyqtSignalInstance()

_PYQT = _mock.MagicMock()
for _sub in ("QtCore", "QtWidgets", "QtGui", "QtSvg", "QtNetwork"):
    setattr(_PYQT, _sub, _mock.MagicMock())
    sys.modules[f"PyQt6.{_sub}"] = getattr(_PYQT, _sub)
_PYQT.QtCore.pyqtSignal = _PyqtSignal
sys.modules["PyQt6"] = _PYQT

del _mock, _PYQT  # Keep _PyqtSignal* classes in namespace for autodoc

# Other packages — mock only in autodoc's scope so Sphinx itself can use them.
autodoc_mock_imports = [
    "pyqtgraph",
    "requests",
    "astropy",
    "astropy.io",
    "astropy.wcs",
    "astropy.coordinates",
    "astropy.time",
    "alpyca",
]

# intersphinx — cross-ref to Python stdlib docs
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "numpy": ("https://numpy.org/doc/stable", None),
    "astropy": ("https://docs.astropy.org/en/stable", None),
}

# -- HTML output ------------------------------------------------------------

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "../tests/**"]

# Don't include test files or conftest.py in autodoc downloads
nitpicky = False

html_theme = "furo"
html_title = f"ARGOS {release}"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_theme_options = {
    "light_css_variables": {
        "color-brand-primary": "#1b4f9c",
        "color-brand-content": "#1b4f9c",
        "font-stack": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif",
    },
    "dark_css_variables": {
        "color-brand-primary": "#6ab0f5",
        "color-brand-content": "#6ab0f5",
    },
    "source_repository": "https://github.com/jperret21/argos/",
    "source_branch": "main",
    "source_directory": "docsrc/",
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/jperret21/argos",
            "html": '<svg stroke="currentColor" fill="currentColor" stroke-width="0" viewBox="0 0 16 16"><path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.012 8.012 0 0 0 16 8c0-4.42-3.58-8-8-8z"></path></svg>',
            "class": "",
        },
    ],
}

# -- Graphviz settings ------------------------------------------------------

graphviz_output_format = "svg"

# -- Extensions -------------------------------------------------------------

# Napoleon settings
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = False
napoleon_include_private_with_doc = False

# autodoc — don't duplicate dataclass fields (attributes + __init__ params)
autoclass_content = 'class'
suppress_warnings = ["misc.highlighting_failure", "ref.duplicate", "toc.circular", "duplicate"]
