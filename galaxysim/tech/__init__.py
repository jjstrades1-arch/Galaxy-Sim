"""Tech as a generative lineage -- ``DESIGN.md`` §1, the game's core mechanism.

Until this existed, ``Civ.techs_known`` was an integer that went up and that
nothing in the simulation read. Research was bought with real materials out of
real colony stockpiles and delivered a counter.
"""

from galaxysim.tech.frontier import (
    FRONTIER_WIDTH,
    ROOT_CONCEPTS,
    ROOT_DOMAINS,
    ROOT_NAME,
    Candidate,
    Known,
    frontier_for,
)
from galaxysim.tech.genome import (
    CONCEPTS,
    CONSTRUCTION,
    DOMAINS,
    DRIVE,
    EMPTY,
    INDUSTRY,
    LIFE_SUPPORT,
    REFINING,
    RESEARCH,
    STATS,
    Effect,
    TechEffects,
    budget,
    combine,
)

__all__ = [
    "CONCEPTS",
    "CONSTRUCTION",
    "Candidate",
    "DOMAINS",
    "DRIVE",
    "EMPTY",
    "Effect",
    "FRONTIER_WIDTH",
    "INDUSTRY",
    "Known",
    "LIFE_SUPPORT",
    "REFINING",
    "RESEARCH",
    "ROOT_CONCEPTS",
    "ROOT_DOMAINS",
    "ROOT_NAME",
    "STATS",
    "TechEffects",
    "budget",
    "combine",
    "frontier_for",
]
