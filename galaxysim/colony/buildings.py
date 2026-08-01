"""The building catalogue.

Hand-authored and fixed, like the world type table. Procedural generation never
touches it, so what a player learns about a Mine stays true across every
playthrough.

Most entries are multipliers on a sector. Three carry structural weight, and
those are the ones that make a colony's build order a real decision rather than
a stat race:

* **Shipyard** gates fleet construction. Without one, a colony cannot build
  ships at all -- so a frontier outpost is not automatically a naval base.
* **Spaceport** gates cargo throughput, which is what lets a colony participate
  in supply routes at any useful rate.
* **Habitat Dome** raises effective habitability, which is the only way to make
  a hostile world cheap to hold rather than permanently expensive.

These compete for the same finite thing: a colony can only staff and site so
much industry at once (:mod:`galaxysim.colony.industry`). Every entry here is an
*industry with levels* rather than a building you either have or do not -- the
costs and work below are for its first level, and each level after that costs
proportionally more and returns proportionally less.

**On the magnitudes.** A level is a substantial installation: tens of thousands
of tonnes and, for a landing party of fifty thousand, a fortnight of everything
it can build with. These numbers are not chosen, they are calibrated -- see
``tests/test_prices.py``, which measures them against a generated capital and a
generated outpost and fails if either end stops making sense. They were three
orders of magnitude lower until the economy was measured and it turned out a
mining district cost thirty tonnes, which is to say nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from galaxysim.colony.labor import AGRICULTURE, EXTRACTION, INDUSTRY, LIFE_SUPPORT, RESEARCH
from galaxysim.materials.catalogue import (
    ALLOYS,
    CERAMICS,
    CONSTRUCTION,
    ELECTRONICS,
    IRON,
    POLYMERS,
    PHOSPHATES,
    SILICON,
    STEEL,
    SULFUR,
    WATER_ICE,
)

#: Capability tokens a building can grant its colony.
FLEET_CONSTRUCTION = "fleet_construction"
CARGO_HANDLING = "cargo_handling"


@dataclass(frozen=True, slots=True)
class BuildingType:
    """One kind of structure."""

    kind: str
    name: str
    description: str
    #: Resources consumed from the host colony's stockpile when work begins.
    cost: dict[str, float]
    #: Industry-work units needed to finish it. Divided by the colony's industry
    #: output, so a colony with nobody in industry never finishes anything.
    work: float

    #: Multiplier added to a labor sector's output, e.g. 0.5 means +50%.
    sector_bonus: dict[str, float] = field(default_factory=dict)
    #: Multiplier added to extraction of one specific material.
    resource_bonus: dict[str, float] = field(default_factory=dict)
    #: Multiplier added to the industry-work this colony can put into refining,
    #: over and above what the sector produces. Ore is worthless until it is
    #: processed, so a colony that mines well and refines badly is sitting on a
    #: pile of rock -- this is the building that fixes that specifically,
    #: without also making everything else build faster.
    refining_bonus: float = 0.0
    #: Added to the world's habitability when computing life-support need. The
    #: only way to make a hostile world genuinely cheap to hold.
    habitability_offset: float = 0.0
    #: Fraction of life-support water consumption recovered by recycling.
    #: Where a dome reduces how much life support is *needed*, this reduces how
    #: much *supply* delivering it burns -- so the two answer different halves
    #: of the same problem and a colony under real pressure wants both.
    life_support_recycling: float = 0.0
    #: Capability tokens this structure grants.
    grants: tuple[str, ...] = ()
    #: Cargo tonnes per hour this colony can load or unload.
    cargo_throughput: float = 0.0


BUILDING_TYPES: tuple[BuildingType, ...] = (
    BuildingType(
        kind="mine",
        name="Mine",
        description="Shafts, draglines and ore processing. Raises the yield of "
        "the structural metals and silicates every chain starts from.",
        cost={STEEL: 30_000, CONSTRUCTION: 25_000},
        work=70,
        resource_bonus={IRON: 0.6, SILICON: 0.3},
    ),
    BuildingType(
        kind="collector",
        name="Volatiles Collector",
        description="Cryogenic traps and regolith bakers. Raises the yield of "
        "ice and silicates -- which on a dry world is the difference between "
        "making your own water and importing every drop of it.",
        cost={STEEL: 25_000, ELECTRONICS: 12_000},
        work=61,
        resource_bonus={WATER_ICE: 0.4, SILICON: 0.3},
    ),
    BuildingType(
        kind="refinery",
        name="Refinery",
        description="Cracking towers and separation trains. Puts more of this "
        "colony's industry into running its refining chains, so the ore it digs "
        "becomes goods it can actually spend.",
        cost={STEEL: 30_000, CERAMICS: 15_000},
        work=70,
        resource_bonus={SULFUR: 0.6, PHOSPHATES: 0.6},
        refining_bonus=0.6,
    ),
    BuildingType(
        kind="factory",
        name="Factory",
        description="Heavy fabrication. Raises industry output, so everything "
        "else on this world gets built faster.",
        cost={STEEL: 45_000, CONSTRUCTION: 30_000, ELECTRONICS: 10_000},
        work=105,
        sector_bonus={INDUSTRY: 0.7},
    ),
    BuildingType(
        kind="shipyard",
        name="Shipyard",
        description="Orbital slipways. Required to build fleets here at all.",
        cost={STEEL: 70_000, ALLOYS: 40_000, ELECTRONICS: 25_000},
        work=175,
        grants=(FLEET_CONSTRUCTION,),
    ),
    BuildingType(
        kind="laboratory",
        name="Laboratory",
        description="Research institutes. Raises research output.",
        cost={STEEL: 25_000, ELECTRONICS: 40_000, CERAMICS: 15_000},
        work=88,
        sector_bonus={RESEARCH: 0.8},
    ),
    BuildingType(
        kind="dome",
        name="Habitat Dome",
        description="Sealed pressurised habitation. Raises effective "
        "habitability, cutting how much work staying alive costs.",
        cost={STEEL: 40_000, POLYMERS: 35_000, CONSTRUCTION: 40_000},
        work=123,
        habitability_offset=0.25,
    ),
    BuildingType(
        kind="hydroponics",
        name="Hydroponics",
        description="Closed-loop food and air. Frees people for other work and "
        "recycles most of the water life support would otherwise burn.",
        cost={CONSTRUCTION: 30_000, POLYMERS: 30_000, ELECTRONICS: 8_000},
        work=79,
        sector_bonus={LIFE_SUPPORT: 1.0, AGRICULTURE: 0.8},
        life_support_recycling=0.5,
    ),
    BuildingType(
        kind="spaceport",
        name="Spaceport",
        description="Bulk cargo handling. Required to load or unload freighters "
        "at any useful rate.",
        cost={STEEL: 50_000, CONSTRUCTION: 45_000, ELECTRONICS: 15_000},
        work=114,
        grants=(CARGO_HANDLING,),
        cargo_throughput=120.0,
    ),
    BuildingType(
        kind="granary",
        name="Reserve Store",
        description="Hardened stores and seed banks. Cheap insurance for a "
        "colony at the end of a long supply line, and a modest help to the "
        "harvest.",
        cost={CONSTRUCTION: 25_000},
        work=44,
        sector_bonus={EXTRACTION: 0.15, AGRICULTURE: 0.2},
        habitability_offset=0.05,
    ),
)

BUILDING_TYPES_BY_KIND: dict[str, BuildingType] = {b.kind: b for b in BUILDING_TYPES}


def building_type(kind: str) -> BuildingType:
    """Look up a building type, raising a helpful error for an unknown kind."""
    try:
        return BUILDING_TYPES_BY_KIND[kind]
    except KeyError:
        known = ", ".join(sorted(BUILDING_TYPES_BY_KIND))
        raise KeyError(f"unknown building kind {kind!r}; known kinds: {known}") from None
