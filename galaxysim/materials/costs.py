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
#:
#: Two hundred thousand tonnes a point, which is to say a warship is a warship:
#: a strength-2 hull masses about what a large ore carrier does. This was
#: nineteen tonnes for two phases, having been left behind when the economy
#: moved to real units -- a civilization producing eighty-six million tonnes an
#: hour bought a capital ship for the price of a lorry, and every constraint
#: downstream of that was fiction.
FLEET_COST_PER_STRENGTH: dict[str, float] = {
    ALLOYS: 63_000.0,
    STEEL: 105_000.0,
    ELECTRONICS: 32_000.0,
}

#: Per tonne of cargo capacity. A freighter is mostly hull: cheap per tonne, and
#: deliberately far cheaper than strength, so hauling is affordable and fighting
#: is not. A big freighter comes out at well under a single point of warship.
FREIGHTER_COST_PER_CAPACITY: dict[str, float] = {
    STEEL: 3.5,
    ALLOYS: 1.0,
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
#:
#: **The split is where this has gone wrong twice, and the reason was the same
#: both times: the bill was anchored to one number and drawn from another.**
#:
#: The first version put five sixths of upkeep into fuel. That looked reasonable
#: -- ships burn fuel -- but fuel is synthesised from water ice and carbon and is
#: also the input to *both* routes to fissiles, so a navy did not merely cost
#: fuel, it consumed the entire chain: no fissiles, no magnetic shields, no
#: terraforming, in a civilization sitting on a hundred million tonnes of alloys.
#:
#: The second version moved to fuel and alloys, kept the total, and was checked
#: against **total industry-work** -- which is neither tonnes nor either of the
#: two materials being billed. Measured properly, at a reference navy of a
#: fortnight of a capital's yard, the fuel line alone came to *185% of every
#: tonne of fuel that capital could make*. It was not payable at any geology.
#: The fleet flew on the fuel its homeworld was seeded with, and when the bank
#: emptied the navy deserted -- which is the "fuel famine" this project has now
#: chased three times, and never once at its cause.
#:
#: So the rule now: **upkeep is billed across the materials a ship actually
#: consumes, each in proportion to what an economy makes of it.** Reaction mass,
#: hull plating, structural spares and thermal ceramics. Measured on a developed
#: capital, each line below comes to about a fifth of that colony's hourly
#: production of that same material -- even across the basket, so no single chain
#: can gate a navy again -- and the whole bill to 7% of refined output, which is
#: the "about a tenth" this constant has always claimed and never met.
#:
#: **Water was in this basket for one draft and came out.** A crew drinks, so it
#: read as the obvious fifth line; the suite caught within a minute that it puts
#: a parked fleet in direct competition with the colonists' life support, drawing
#: down the one material a colony dies without. That is the same single-chain
#: gate this constant is trying to stop having, aimed at something worse than a
#: navy. Upkeep is billed in industrial goods only.
#:
#: Poor geology still bites, and that is the point rather than a flaw. Across
#: five seeded homeworlds the reference navy takes 10-26% of each chain that
#: feeds it, except on the one drawn short of both carbon and iron, where fuel
#: and steel run to 75% and 71%. That world's owner has a decision -- go and
#: settle carbon, or fly a smaller navy -- rather than a countdown, because
#: nothing is above 100% anywhere and
#: :func:`~galaxysim.engine.resolvers.production._charge_fleet_upkeep` scales
#: desertion to the unpaid share of the *whole* bill rather than to its worst
#: line. Being short of one material costs a fleet that material's share of
#: itself and not the fleet.
FLEET_UPKEEP_PER_STRENGTH: dict[str, float] = {
    FUEL: 120.0,
    ALLOYS: 1_030.0,
    STEEL: 400.0,
    CERAMICS: 1_450.0,
}

#: What a colony pod costs to build, and the work of assembling one.
#:
#: **This was free for the entire life of the project**, and it set the pace of
#: the whole game by accident. ``Fleet.colony_pods`` was an integer nobody
#: charged for: you ordered a strength-2 hull, ticked the box, and paid for a
#: gunboat. So how fast a civilization expanded was decided by the build time of
#: a *warship* -- nine hours of a capital's construction -- and AI empires
#: founded a new world every ten hours, forever, reaching five hundred colonies
#: in four weeks while their population moved four percent. Wide and hollow.
#:
#: A pod is not a cargo container. It is everything fifty thousand people need
#: to be self-sufficient on a world that has never held life: pressure vessels,
#: reactors, foundries, soil, seed stock, the machines that make the machines.
#: Priced as such, it is the most expensive single object a young civilization
#: builds, and rightly -- founding a world should be the decision of a season,
#: not of an afternoon.
#:
#: The **work** is what binds. Materials a capital replaces in an hour; a
#: shipyard cannot be hurried, and that is the honest constraint on expansion.
#: Calibrated in ``tests/test_prices.py`` against a measured capital so it says
#: what it means: **about four days a world** for a young civilization, and
#: faster as its industry deepens -- so the frontier accelerates because the
#: empire got stronger, not because a rule let go.
#:
#: That figure was five days until construction started receiving the industry
#: refining could not spend, which is worth recording because the temptation was
#: to price this constant up by a fifth and put the curve back where it was. The
#: pacing property is the *shape* of the curve rather than any number in it, and
#: hiding a corrected economy behind a re-tuned price is the mistake this file
#: keeps documenting.
COLONY_POD_COST: dict[str, float] = {
    STEEL: 1_200_000.0,
    ALLOYS: 450_000.0,
    ELECTRONICS: 300_000.0,
    CONSTRUCTION: 900_000.0,
    POLYMERS: 150_000.0,
    CERAMICS: 100_000.0,
}

#: Industry-work to assemble one pod. The brake on how fast anyone expands.
COLONY_POD_WORK = 450_000_000.0

#: Fraction of a hull's build cost recovered when it is broken up at a colony.
#:
#: Not all of it, because a ship is not a pile of its inputs -- the shaping, the
#: electronics and the labour do not come back. Not none of it either: a hull is
#: mostly structural metal and that metal is still there. A third is enough to
#: make scrapping a real decision (it returns materials *and* stops the bill)
#: without making a fleet a savings account you can cash out at will.
SALVAGE_FRACTION = 1.0 / 3.0

# --- expeditions -------------------------------------------------------------

#: Per colonist -- and a colonist is now one person, so these are tonnes per
#: head: shelter to build, food and water for the crossing and the first months.
#: Three tonnes a person needs no scaling apology; it is about right.
COLONIST_COST: dict[str, float] = {CONSTRUCTION: 0.9, FOOD: 1.2, WATER: 1.0}

#: Per unit of equipment, which becomes the colony's starting infrastructure.
#:
#: Priced against a level of industry rather than against a crate, because that
#: is what it becomes: a unit of equipment is a working installation on landing,
#: and one that cost nineteen tonnes made a colony's entire industrial base
#: cheaper than the food its settlers ate on the way.
EQUIPMENT_COST: dict[str, float] = {
    STEEL: 12_000.0,
    ALLOYS: 4_500.0,
    ELECTRONICS: 3_000.0,
    CONSTRUCTION: 9_000.0,
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
