"""How big a colony's industries can get, and what that costs.

Slots are gone. A world used to allow "four buildings", which said nothing about
the world and nothing about the colony -- a mining town of thirty thousand and a
planet of nine billion got the same four, and a gas giant and a continent got
the same four.

What replaces it is two physical limits, and both of them move as the colony
develops:

**Staffing.** An industry needs people to run it. A colony of three million
cannot operate the mining sector of a colony of three billion, however much ore
is under it and however much steel it has banked.

**Ground.** Industry occupies land. A small moon runs out of surface long before
it runs out of people, which is what stops a cramped body from ever becoming an
industrial centre no matter how many migrants are shipped in.

Whichever binds first is the limit, and a colony feels quite different depending
on which one it is: a crowded moon wants migrants it cannot use, a vast empty
continent wants people it does not have.

Levels cost more as they rise and return less, so nothing runs away:

* **Cost scales with the square of the level being built**, so the tenth level
  of a mine costs a hundred times the first and total investment is cubic.
* **Effect scales with the square root of level**, so a level-100 mine is ten
  times a level-1 mine rather than a hundred times.

The gap between those two is enormous on purpose, because the economies it has
to span are: a landing party of fifty thousand has to be able to afford level 1,
and a civilization of eighteen billion has to find level 300 a real commitment.

The consequence is the one the design wants. Past a point the next tonne of steel
is always better spent founding a *new* colony than deepening an old one, so
expansion is the reward for growing -- and nothing had to be written down to make
it true.
"""

from __future__ import annotations

import math

#: People needed to staff one level of one industry. A level is a substantial
#: installation -- a mining district, a shipyard complex -- not a building.
STAFF_PER_LEVEL = 250_000.0

#: Square kilometres of usable land one level occupies. Generous: this binds
#: only on genuinely small bodies, which is the intent. A continent-sized world
#: is limited by its people; a moon is limited by its ground.
KM2_PER_LEVEL = 90_000.0

#: Every colony can run at least this much industry, however tiny. Without it a
#: landing party could never build the thing that lets it grow, and a new colony
#: would be a permanent hole in the ground.
MINIMUM_LEVELS = 1


def staffing_limit(population: float) -> float:
    return max(0.0, population) / STAFF_PER_LEVEL


def land_limit(land_area_km2: float) -> float:
    return max(0.0, land_area_km2) / KM2_PER_LEVEL


def max_total_levels(population: float, land_area_km2: float) -> int:
    """Total industry levels this colony can support across all its sectors."""
    physical = min(staffing_limit(population), land_limit(land_area_km2))
    return max(MINIMUM_LEVELS, int(physical))


def binding_limit(population: float, land_area_km2: float) -> str:
    """Which of the two is actually holding this colony back. For display."""
    if staffing_limit(population) <= land_limit(land_area_km2):
        return "people"
    return "ground"


def levels_in_use(buildings) -> int:
    """Levels already committed, counting industries still under construction."""
    return sum(max(1, building.level) for building in buildings)


#: How sharply the cost of the next level rises. Quadratic in the level being
#: built, so *total* investment is cubic while the effect only grows as the
#: square root.
#:
#: That spread is doing real work. A level-1 industry has to be affordable to
#: fifty thousand colonists with hand tools; a level-300 one has to be a
#: meaningful commitment for a civilization of eighteen billion. Those two
#: economies differ by seven orders of magnitude, and a gentler curve cannot
#: span them -- either the frontier can never build anything or the capital
#: maxes out its world in an afternoon.
#:
#: The consequence is the one the design wants: past a point, the next tonne of
#: steel is always better spent founding a new colony than deepening an old one.
#: Expansion is the reward for growing, and nothing had to be written down to
#: make it so.
LEVEL_COST_EXPONENT = 2.0


def _level_factor(level: int) -> float:
    return float(max(1, level)) ** LEVEL_COST_EXPONENT


def cost_of_level(base_cost: dict[str, float], level: int) -> dict[str, float]:
    """Materials for the ``level``-th level of an industry."""
    factor = _level_factor(level)
    return {key: amount * factor for key, amount in base_cost.items()}


def work_of_level(base_work: float, level: int) -> float:
    """Industry-work for the ``level``-th level. Same curve as the materials."""
    return base_work * _level_factor(level)


def effect_scale(level: int) -> float:
    """Multiplier on a building's stated bonus at ``level``.

    Square root: a level-100 mine is ten mines' worth of effect, not a hundred.
    Paired with linear cost this gives development sharply diminishing returns,
    which is what keeps deepening one colony from beating founding another.
    """
    return math.sqrt(max(1, level))


def infrastructure_of(buildings) -> float:
    """A colony's infrastructure: what its finished industries add up to.

    Replaces the hand-set float that used to sit on the colony -- infrastructure
    is now something you can point at, namely the industries standing on the
    ground. It sizes the habitat ceiling on a dead world
    (:func:`galaxysim.colony.population.habitat_capacity`) and it is displayed.

    It deliberately does *not* multiply output: that is
    :func:`productivity`'s job, and letting both scale with level made the two
    compound. Production maintains the figure incrementally as levels finish;
    this is the bulk form, used when a colony is created with industry already
    standing.
    """
    return sum(
        effect_scale(building.level) for building in buildings if building.is_complete
    )


#: Output multiplier at zero development and at full. A colony that has built
#: nothing still works -- people can dig with their hands -- and one that has
#: built everything its world allows is worth about five times that.
#:
#: **Bounded on purpose.** Industry improves output through its *sector bonuses*,
#: which already scale with level. If the same levels also fed an unbounded
#: multiplier here, the two would compound: an early version had a capital's
#: research running a hundred times faster than intended because a level-277
#: laboratory multiplied a level-277 infrastructure figure. Development is a
#: measure of how well equipped the workforce is, and there is a limit to how
#: much equipment helps.
UNDEVELOPED_PRODUCTIVITY = 0.3
DEVELOPED_PRODUCTIVITY = 1.5


def development(levels_built: int, ceiling: int) -> float:
    """How built-out a colony is, 0 to 1.

    Levels standing against levels the world and the population could support.
    Scale-free by construction, which matters: infrastructure per head is not,
    because a small colony's minimum viable industry is a much larger share of
    its people than a large colony's.
    """
    if ceiling <= 0:
        return 0.0
    return min(1.0, max(0.0, levels_built) / ceiling)


def productivity(development_ratio: float) -> float:
    """The output multiplier a given level of development buys."""
    span = DEVELOPED_PRODUCTIVITY - UNDEVELOPED_PRODUCTIVITY
    return UNDEVELOPED_PRODUCTIVITY + span * min(1.0, max(0.0, development_ratio))
