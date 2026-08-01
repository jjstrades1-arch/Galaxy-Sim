"""What things cost, in real materials.

One place for every price in the game, so the economy can be read and rebalanced
without hunting through resolvers.

The research prices carry a design constraint worth stating, because it is easy
to get backwards. Research consumes **common industrial goods** — things every
civilization can make by more than one route — and never rare strategic ones.
That keeps tech costing something real without letting geology gate it: a civ
that draws a metal-poor start must work harder, but it is never locked out of a
branch because its crust lacks an element. Rare materials only ever *accelerate*
(see :data:`RESEARCH_ACCELERANTS`), which is optional by construction.
"""

from __future__ import annotations

from galaxysim.materials.catalogue import (
    ALLOYS,
    CERAMICS,
    CONSTRUCTION,
    ELECTRONICS,
    FISSILES,
    FOOD,
    FUEL,
    HELIUM3,
    POLYMERS,
    RARE_EARTHS,
    STEEL,
    WATER,
)

# --- ships -------------------------------------------------------------------

#: Tonnes per point of fleet strength. Warships are alloy and electronics heavy.
#:
#: Strength has no absolute meaning -- combat compares one fleet's to another's --
#: so it is allowed to grow with the civilization building it. A homeworld of
#: seven billion fields strength in the thousands; a frontier outpost fields
#: single digits. What keeps that honest is upkeep, below.
FLEET_COST_PER_STRENGTH: dict[str, float] = {
    ALLOYS: 6.0,
    STEEL: 10.0,
    ELECTRONICS: 3.0,
}

#: Per tonne of cargo capacity. A freighter is mostly hull: cheap per tonne, and
#: deliberately far cheaper than strength, so hauling is affordable and fighting
#: is not.
FREIGHTER_COST_PER_CAPACITY: dict[str, float] = {
    STEEL: 0.35,
    ALLOYS: 0.1,
}

#: Ongoing, per point of fleet strength per real hour. Without upkeep a fleet is
#: free once built, hoarding is strictly correct, and a civilization accumulates
#: ships without limit.
#:
#: Set at roughly 1.5% of build cost per hour, so a ship costs as much to keep
#: for three days as it did to build. That *ratio* is what bounds a navy rather
#: than any absolute figure -- strength is allowed to grow with the civilization
#: building it, and upkeep grows with it, so a civ can sustain a fleet drawing
#: about a tenth of its refined output whatever its size.
FLEET_UPKEEP_PER_STRENGTH: dict[str, float] = {
    FUEL: 0.24,
    ALLOYS: 0.05,
}

# --- expeditions -------------------------------------------------------------

#: Per colonist -- and a colonist is now one person, so these are tonnes per
#: head: shelter to build, food and water for the crossing and the first months.
#: Three tonnes a person needs no scaling apology; it is about right.
COLONIST_COST: dict[str, float] = {CONSTRUCTION: 0.9, FOOD: 1.2, WATER: 1.0}

#: Per unit of equipment, which becomes the colony's starting infrastructure.
EQUIPMENT_COST: dict[str, float] = {
    STEEL: 8.0,
    ALLOYS: 3.0,
    ELECTRONICS: 2.0,
    CONSTRUCTION: 6.0,
}

#: Per unit of life-support stores — the colony's survival clock on a world that
#: cannot supply its own air and water.
STORES_COST: dict[str, float] = {WATER: 0.6, FOOD: 0.5, POLYMERS: 0.1}

# --- research ----------------------------------------------------------------

#: Consumed per unit of research progress. Deliberately common goods with more
#: than one production route apiece, so no crust composition can lock a
#: civilization out of researching at all.
RESEARCH_COST_PER_PROGRESS: dict[str, float] = {
    ELECTRONICS: 0.55,
    POLYMERS: 0.25,
    CERAMICS: 0.2,
    FUEL: 0.15,
}

#: Optional inputs that speed research when supplied, and cost nothing but the
#: material when they are not. This is where rare materials earn their place in
#: the tech economy without ever becoming a requirement.
#:
#: Value is the multiplier on research rate at full supply.
RESEARCH_ACCELERANTS: dict[str, float] = {
    RARE_EARTHS: 1.35,
    FISSILES: 1.5,
    HELIUM3: 1.6,
}

#: How much accelerant a unit of research progress can absorb. Beyond this the
#: extra material does nothing, so stockpiling accelerants is not a strategy.
ACCELERANT_PER_PROGRESS = 0.2


def accelerant_multiplier(stock: dict[str, float], progress: float) -> tuple[float, dict]:
    """Best speed-up available from what this colony happens to hold.

    Returns the multiplier and the manifest to consume. Only the single best
    accelerant is used -- they do not stack, because three multiplicative
    bonuses would quickly outrun the cost curve they are supposed to sit under.
    """
    if progress <= 0:
        return 1.0, {}

    wanted = progress * ACCELERANT_PER_PROGRESS
    best_key, best_multiplier, best_amount = None, 1.0, 0.0

    for key, multiplier in sorted(RESEARCH_ACCELERANTS.items()):
        available = min(stock.get(key, 0.0), wanted)
        if available <= 0:
            continue
        # Partial supply gives a partial speed-up, so a trickle still helps.
        share = available / wanted
        effective = 1.0 + (multiplier - 1.0) * share
        if effective > best_multiplier:
            best_key, best_multiplier, best_amount = key, effective, available

    if best_key is None:
        return 1.0, {}
    return best_multiplier, {best_key: best_amount}
