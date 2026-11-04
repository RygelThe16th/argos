"""Star catalogue access for photometry and solved-field identification.

Qt-free clients for the two AAVSO services this app cares about:

* **VSX** (Variable Star indeX) — *where the variable stars are* in a field.
* **VSP** (Variable Star Plotter) — *which comparison stars* are available,
  with calibrated magnitudes, for photometry of a target.
* **Gaia DR3** — stable source identifiers for ordinary stars in a solved field.

The UI solves a frame (:mod:`argos.core.imaging.platesolve`), gets a
:class:`~argos.core.imaging.platesolve.FrameWCS`, then queries these by the
field centre + radius and projects the results back onto the image.
"""

from __future__ import annotations

from .aavso import (
    Band,
    CatalogError,
    ComparisonStar,
    VariableStar,
    vsp_chart,
    vsx_cone_search,
)
from .gaia import GaiaStar, gaia_cone_search
from .exoplanets import (
    CachedExoplanetHost,
    cached_exoplanet_hosts_in_cone,
    exoplanet_hosts_in_cone,
)
from .field_objects import FieldObjectLookupError, NamedFieldObject, simbad_field_objects
from .point_identity import (
    PointIdentityLookupError,
    PointSourceIdentity,
    identify_point_source,
)
from .photometry import (
    ComparisonExclusion,
    ComparisonQuality,
    ComparisonSelection,
    FieldPlacement,
    ScoredComparison,
    auto_comparison_stars,
    comparisons_for_variable,
    field_placement,
    rank_comparisons,
    select_comparison_stars,
    separation_arcmin,
)

__all__ = [
    "Band",
    "CatalogError",
    "ComparisonStar",
    "ComparisonExclusion",
    "ComparisonQuality",
    "ComparisonSelection",
    "FieldPlacement",
    "ScoredComparison",
    "GaiaStar",
    "VariableStar",
    "auto_comparison_stars",
    "comparisons_for_variable",
    "field_placement",
    "select_comparison_stars",
    "gaia_cone_search",
    "CachedExoplanetHost",
    "cached_exoplanet_hosts_in_cone",
    "exoplanet_hosts_in_cone",
    "FieldObjectLookupError",
    "NamedFieldObject",
    "PointIdentityLookupError",
    "PointSourceIdentity",
    "rank_comparisons",
    "separation_arcmin",
    "simbad_field_objects",
    "identify_point_source",
    "vsp_chart",
    "vsx_cone_search",
]
