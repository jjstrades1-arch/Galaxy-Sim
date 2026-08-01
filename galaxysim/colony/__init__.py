"""The colony interior: labor sectors and the building catalogue.

A colony is the unit of play the economy is built around. This package holds the
hand-authored pieces of that -- which jobs exist, which structures exist, and
what they do. Nothing here is procedurally generated, on the same principle as
:mod:`galaxysim.worldgen.types`: what a player learns about a Mine has to stay
true.
"""

from galaxysim.colony.buildings import (
    BUILDING_TYPES,
    BUILDING_TYPES_BY_KIND,
    BuildingType,
    building_type,
)
from galaxysim.colony.labor import (
    EXTRACTION,
    INDUSTRY,
    LIFE_SUPPORT,
    RESEARCH,
    SECTORS,
    balanced_allocation,
    normalize,
    workers_in,
)

__all__ = [
    "BUILDING_TYPES",
    "BUILDING_TYPES_BY_KIND",
    "BuildingType",
    "building_type",
    "EXTRACTION",
    "INDUSTRY",
    "RESEARCH",
    "LIFE_SUPPORT",
    "SECTORS",
    "balanced_allocation",
    "normalize",
    "workers_in",
]
