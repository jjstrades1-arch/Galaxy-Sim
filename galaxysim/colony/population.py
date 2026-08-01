"""How many people a world holds, and how fast that number moves.

Two regimes, and the gap between them is what terraforming exists to close.

**A living world is limited by its land.** Real surface area at a real
population density, scaled by how pleasant the place is:
``land_area_km2 x 53 x habitability``, computed in
:attr:`galaxysim.worldgen.survey.Survey.carrying_capacity`. An Earth-analogue
lands near thirteen billion; an ocean world with barely any ground near a
hundred million.

**A dead world is limited by what you built.** Habitability near zero means
natural capacity near zero -- nobody lives outside on Mars -- so the ceiling is
whatever the habitats hold, which is *tens of thousands on landing rising toward
a few million*. Three orders of magnitude below a real world, permanently, until
somebody changes the planet.

That gap is the entire argument for terraforming. A dead world is an industrial
installation; a living one is a civilization. Raising habitability does not
improve an outpost, it *converts* it.

The growth rate deserves a note because it is not biological. A frontier colony
matures in three to four weeks of wall-clock time, which is far faster than any
real population grows. That is a deliberate compression of the game's timescale,
and the design's answer to it is migration: shipping a million people is exactly
as plausible as shipping a million tonnes, so a player who runs population
convoys is doing something real rather than waiting out a number.
"""

from __future__ import annotations

#: People a single unit of habitat infrastructure supports on a world that
#: cannot support anyone outdoors. Sealed volume is expensive: this is what
#: keeps a dead world an outpost no matter how long it is left alone.
HABITAT_CAPACITY_PER_INFRASTRUCTURE = 120_000.0

#: Above this habitability a world's own land is what limits it, and the
#: artificial floor stops applying. Below it, people live indoors.
LIVEABLE_THRESHOLD = 0.12


def natural_capacity(world) -> float:
    """People the world's own surface supports, from its stored survey.

    Zero on a dead world, and that is the point -- there is no floor here. The
    floor lives in :func:`habitat_capacity`, where it belongs, because it is a
    fact about what you built rather than about the planet.
    """
    return max(0.0, float(world.carrying_capacity or 0.0))


def habitat_capacity(infrastructure: float) -> float:
    """People the colony's sealed habitats support, regardless of the world."""
    return max(0.0, infrastructure) * HABITAT_CAPACITY_PER_INFRASTRUCTURE


def capacity(world, infrastructure: float, habitability: float | None = None) -> float:
    """The ceiling this colony is actually growing toward.

    Whichever regime is larger. On a garden world the land wins by a wide margin
    and habitats are irrelevant; on a barren rock the land contributes nothing
    and the habitats are the whole story.
    """
    effective = world.habitability if habitability is None else habitability
    natural = natural_capacity(world) if effective >= LIVEABLE_THRESHOLD else 0.0
    return max(natural, habitat_capacity(infrastructure))


def growth_per_hour(
    population: float,
    ceiling: float,
    *,
    base_rate: float,
    standard_of_living: float = 1.0,
    hazard: float = 0.0,
) -> float:
    """Logistic growth, which reproduces something real without special casing.

    A frontier colony with room booms; a world already at capacity barely moves.
    So a homeworld opening at 80% of its ceiling grows a few percent a month and
    essentially all of a civilization's growth has to come from expanding -- the
    decision the opening position is meant to force.

    Over capacity the same formula runs backwards, so losing habitability
    actually costs people rather than pinning the number where it was.
    """
    if population <= 0:
        return 0.0
    if ceiling <= 0:
        # Nowhere to live at all. Such a colony only exists while people are
        # shipped in, and it shrinks the moment they stop.
        return -population * base_rate

    headroom = 1.0 - (population / ceiling)
    if headroom <= 0:
        return population * base_rate * headroom

    return population * base_rate * headroom * standard_of_living * (1.0 - hazard)
