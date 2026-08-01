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

Worlds have a limited number of slots (see :attr:`World.slots`), so these
compete. A gas giant with three slots cannot have a dome, a shipyard and a full
mining chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from galaxysim.colony.labor import EXTRACTION, INDUSTRY, LIFE_SUPPORT, RESEARCH
from galaxysim.core.resources import ENERGY, METAL, VOLATILES

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
    #: Multiplier added to extraction of one specific resource.
    resource_bonus: dict[str, float] = field(default_factory=dict)
    #: Added to the world's habitability when computing life-support need. The
    #: only way to make a hostile world genuinely cheap to hold.
    habitability_offset: float = 0.0
    #: Fraction of life-support volatiles consumption recovered by recycling.
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
        description="Deep extraction works. Raises metal yield.",
        cost={METAL: 40.0},
        work=8.0,
        resource_bonus={METAL: 0.6},
    ),
    BuildingType(
        kind="collector",
        name="Energy Collector",
        description="Orbital and surface collection arrays. Raises energy yield.",
        cost={METAL: 30.0, ENERGY: 15.0},
        work=7.0,
        resource_bonus={ENERGY: 0.6},
    ),
    BuildingType(
        kind="refinery",
        name="Refinery",
        description="Volatile processing and cracking. Raises volatiles yield.",
        cost={METAL: 35.0, ENERGY: 10.0},
        work=8.0,
        resource_bonus={VOLATILES: 0.6},
    ),
    BuildingType(
        kind="factory",
        name="Factory",
        description="Heavy fabrication. Raises industry output, so everything "
        "else on this world gets built faster.",
        cost={METAL: 50.0, ENERGY: 20.0},
        work=12.0,
        sector_bonus={INDUSTRY: 0.7},
    ),
    BuildingType(
        kind="shipyard",
        name="Shipyard",
        description="Orbital slipways. Required to build fleets here at all.",
        cost={METAL: 80.0, ENERGY: 40.0},
        work=20.0,
        grants=(FLEET_CONSTRUCTION,),
    ),
    BuildingType(
        kind="laboratory",
        name="Laboratory",
        description="Research institutes. Raises research output.",
        cost={METAL: 45.0, ENERGY: 25.0},
        work=10.0,
        sector_bonus={RESEARCH: 0.8},
    ),
    BuildingType(
        kind="dome",
        name="Habitat Dome",
        description="Sealed pressurised habitation. Raises effective "
        "habitability, cutting how much work staying alive costs.",
        cost={METAL: 60.0, VOLATILES: 30.0},
        work=14.0,
        habitability_offset=0.25,
    ),
    BuildingType(
        kind="hydroponics",
        name="Hydroponics",
        description="Closed-loop food and air. Frees people for other work and "
        "recycles most of the volatiles life support would otherwise burn.",
        cost={METAL: 30.0, VOLATILES: 40.0},
        work=9.0,
        sector_bonus={LIFE_SUPPORT: 1.0},
        life_support_recycling=0.5,
    ),
    BuildingType(
        kind="spaceport",
        name="Spaceport",
        description="Bulk cargo handling. Required to load or unload freighters "
        "at any useful rate.",
        cost={METAL: 55.0, ENERGY: 30.0},
        work=13.0,
        grants=(CARGO_HANDLING,),
        cargo_throughput=25.0,
    ),
    BuildingType(
        kind="granary",
        name="Reserve Store",
        description="Hardened stores. Cheap insurance for a colony at the end "
        "of a long supply line.",
        cost={METAL: 20.0},
        work=5.0,
        sector_bonus={EXTRACTION: 0.15},
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
