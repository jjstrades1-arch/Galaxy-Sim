"""Power: a flow, not a stockpile.

Everything else a colony holds is a pile of stuff that sits there until it is
spent. Electricity is not like that and pretending otherwise was always going to
read wrong: you generate it and you use it in the same instant, and if you
generate less than you need the lights do not go out, the smelters run slow.

So power is the one quantity in the game that is a **rate compared against a
rate**. Generation is what the plants can make this hour; demand is what the
colony's industry, mines and life support want this hour; and the ratio between
them scales output. A colony short of power is not dead, it is *throttled*, and
it recovers the moment somebody delivers fuel.

**Why this exists at all.** Extraction had no sink. A developed civilization
accumulated thirty-five million tonnes of silicon an hour with nothing that
consumed it, which made half the economy decorative -- and deuterium and
helium-3, catalogued as strategic fusion fuels and named the best research
accelerants in the game, were mined by nobody because nothing burned them.
``materials/recipes.py`` has carried a comment pointing at "the energy model"
since the refining chains were written. This is that model.

**Why it makes worlds different.** Every generation route is scaled by something
the survey already knows, so where a colony sits decides how it keeps its lights
on:

* **Solar** by stellar flux. Real: ``L / a**2``. A world tucked close to a
  bright star is drowning in it; a rock in the outer system of an M dwarf sees
  almost nothing, and three quarters of stars are M dwarfs.
* **Fission** by uranium in the stockpile, refined into fissiles.
* **Fusion** by deuterium and helium-3 -- rare, enormously energy-dense, and the
  reason a regolith world nobody would otherwise settle is worth having.
* **Geothermal** by tectonic activity, which is also what concentrates ore. So
  the volcanic worlds that are the best mining targets are the ones that can
  most easily power the mine, and the dead ones that are cheap to live on are
  the ones that must import their fuel.

None of that needed a rule. It is four real physical quantities the generator
was already producing, read by four kinds of power plant.
"""

from __future__ import annotations

from galaxysim.materials.catalogue import DEUTERIUM, FISSILES, HELIUM3

#: Power units one level of a generator produces per real hour at its best.
#:
#: The unit is arbitrary and deliberately so -- what matters is the ratio
#: against :data:`DEMAND_PER_WORKER_HOUR` below, since nothing outside this
#: module ever sees an absolute figure. One unit is roughly what it takes to run
#: a thousand industrial workers.
SOLAR_PER_LEVEL = 7_800.0
FISSION_PER_LEVEL = 27_000.0
FUSION_PER_LEVEL = 78_000.0
GEOTHERMAL_PER_LEVEL = 19_500.0

#: Flux at which a solar farm hits its rated output, relative to Earth's. Below
#: this it scales linearly; above it there is nothing more to collect, because
#: the limiting factor stops being sunlight and becomes panel area.
SOLAR_REFERENCE_FLUX = 1.0

#: Tonnes of fuel burned per hour per unit of power, by route. Fusion fuels are
#: absurdly energy-dense next to fission -- which is the real relationship, and
#: what makes a helium-3 world worth crossing a frontier for.
FUEL_PER_POWER_HOUR: dict[str, float] = {
    FISSILES: 4.0e-3,
    DEUTERIUM: 9.0e-5,
    HELIUM3: 6.0e-5,
}

#: Power a colony wants per unit of industrial *output*, not per worker.
#:
#: That distinction is the whole mechanic. Demand scaled to headcount would grow
#: exactly as fast as the baseline below and power would never bind on anybody.
#: Scaled to output it grows with everything that makes a colony productive --
#: deeper industries, better development, a workforce with real plant behind it
#: -- while the baseline only grows with people. So a landing party never thinks
#: about electricity and a developed world has to build for it, which is both
#: the right pressure and what actually happens.
DEMAND_PER_INDUSTRY_WORK = 1.0
DEMAND_PER_EXTRACTION_TONNE = 0.05

#: Power per person per hour to keep them alive, scaled by how hostile the world
#: is. Nothing on a garden world; on an airless rock it is pumps and heaters and
#: lights running continuously, and it never stops.
DEMAND_PER_SEALED_PERSON_HOUR = 4.0e-5

#: What a colony generates without building anything: the reactors that came
#: with the expedition and the supply every installation carries for itself.
#:
#: Scaled to population, so it covers a small colony completely and a developed
#: one only partly. A civilization does not stop being able to boil a kettle
#: because it never built a power station; what it cannot do without one is run
#: continent-scale smelting.
BASELINE_POWER_PER_PERSON = 2.0e-4

#: The floor a brownout can throttle output to. Not zero: a colony that lost all
#: power would be unrecoverable, since it could not mine the fuel to restart --
#: and a mechanic that can put a colony into a state it cannot leave is a trap
#: rather than a constraint.
MINIMUM_SATISFACTION = 0.15


def solar_output(levels: float, stellar_flux: float) -> float:
    """What collectors make here. Linear in sunlight, and it runs out."""
    if levels <= 0:
        return 0.0
    share = min(1.0, max(0.0, stellar_flux) / SOLAR_REFERENCE_FLUX)
    return levels * SOLAR_PER_LEVEL * share


def geothermal_output(levels: float, tectonic_activity: float) -> float:
    """What the interior gives up. Free, endless, and only on a live world."""
    if levels <= 0:
        return 0.0
    return levels * GEOTHERMAL_PER_LEVEL * min(1.0, max(0.0, tectonic_activity))


def fuelled_capacity(fission_levels: float, fusion_levels: float) -> float:
    """What the fuelled plants *could* make, before asking whether fuel exists."""
    return max(0.0, fission_levels) * FISSION_PER_LEVEL + (
        max(0.0, fusion_levels) * FUSION_PER_LEVEL
    )


def baseline_output(population: float) -> float:
    """What the colony makes without having built a power station."""
    return max(0.0, population) * BASELINE_POWER_PER_PERSON


def demand(
    industry_work: float,
    extraction_tonnes: float,
    population: float,
    effective_habitability: float,
) -> float:
    """What this colony wants this hour, given what it is trying to produce.

    Both industrial terms are what the colony *could* make at full power, not
    what it will end up making -- otherwise demand depends on satisfaction and
    satisfaction depends on demand, and a brownout would quietly justify itself
    by reducing the load that caused it.

    Two halves with quite different characters. The industrial half is a
    *choice*: put fewer people in the smelters and the demand falls. The
    life-support half is not. It scales with how many people are here and how
    hostile the world is, and on a sealed world it is the bill that arrives
    whether or not anything is being produced.
    """
    hostility = min(1.0, max(0.0, 1.0 - effective_habitability))
    return (
        max(0.0, industry_work) * DEMAND_PER_INDUSTRY_WORK
        + max(0.0, extraction_tonnes) * DEMAND_PER_EXTRACTION_TONNE
        + max(0.0, population) * hostility * DEMAND_PER_SEALED_PERSON_HOUR
    )


def satisfaction(generation: float, wanted: float) -> float:
    """How much of what the colony wanted it got, as a multiplier on output.

    A colony that wants nothing is perfectly satisfied, which matters more than
    it sounds: it is what stops a fresh landing party with nobody assigned to
    industry from being reported as in a blackout.
    """
    if wanted <= 0.0:
        return 1.0
    if generation <= 0.0:
        return MINIMUM_SATISFACTION
    return max(MINIMUM_SATISFACTION, min(1.0, generation / wanted))


def fuel_draw(power_from_fuel: float, hours: float, stockpile: dict) -> dict[str, float]:
    """Which fuels to burn for ``power_from_fuel`` units over ``hours``.

    Cheapest-first by mass, which comes out as fusion before fission wherever
    fusion fuel exists -- correct, because a tonne of helium-3 is worth something
    like seventy tonnes of fissiles and a colony would obviously burn it first.
    Returns what it wants; the caller decides what it can actually afford.
    """
    if power_from_fuel <= 0 or hours <= 0:
        return {}

    wanted = power_from_fuel * hours
    draw: dict[str, float] = {}
    for fuel in sorted(FUEL_PER_POWER_HOUR, key=lambda f: FUEL_PER_POWER_HOUR[f]):
        if wanted <= 1e-12:
            break
        per_unit = FUEL_PER_POWER_HOUR[fuel]
        available = max(0.0, float(stockpile.get(fuel, 0.0)))
        if available <= 0:
            continue
        units = min(wanted, available / per_unit)
        if units <= 0:
            continue
        draw[fuel] = units * per_unit
        wanted -= units
    return draw


def power_from_fuel_available(stockpile: dict, hours: float) -> float:
    """The most the fuelled plants could run on what is in the warehouse."""
    if hours <= 0:
        return 0.0
    return sum(
        max(0.0, float(stockpile.get(fuel, 0.0))) / (per_unit * hours)
        for fuel, per_unit in FUEL_PER_POWER_HOUR.items()
        if per_unit > 0
    )
