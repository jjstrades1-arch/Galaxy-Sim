"""Choosing what to do to a planet next.

Terraforming has to be driven by feedback rather than a recipe, because the
projects interact. Greenhouse forcing scales with pressure, so thickening the
air warms a world *as well as* pressurising it; scrubbing sulphur back out thins
the air again and may drop it below working pressure; and three greenhouse
seedings on a world already warmed by three atmosphere processors overshoot the
growing band and cook it. **A fixed sequence that is right for one planet is
wrong for the next one.**

So this reads the survey and picks, the way a player looking at the readout
would. What it encodes is not an order of operations but a set of things that
have to become true, checked against what is currently true -- which is why the
same function serves a player asking "what now", the AI running a campaign, and
the tests asserting that the physics converges at all.
"""

from __future__ import annotations

from galaxysim.terraform.projects import Project, project

#: The surface temperature a terraformer is aiming at: warm enough to grow food
#: in, cool enough to stand in. Deliberately a band rather than a target,
#: because both of the tools that move temperature are coarse.
GROWING_BAND_K = (283.0, 303.0)

#: Enough air to breathe and to hold heat, below which nothing else is worth
#: doing. Roughly Earth's, which is the only figure anyone has ever tested.
WORKING_PRESSURE_BAR = 0.7

#: Biosphere stages that still want seeding. Past these a world's own ecology
#: is oxygenating the sky faster than a plant could.
UNFINISHED_LIFE = ("sterile", "prebiotic", "microbial")


def next_project(survey) -> str | None:
    """The project this world needs next, or ``None`` if it is finished.

    Ordered by what blocks what, not by cost: there is no point releasing an
    atmosphere onto a world with no magnetic field to hold it down, and no point
    seeding an ecosystem into air a bacterium could not survive.
    """
    air, climate, water, life = (
        survey.atmosphere,
        survey.climate,
        survey.hydrosphere,
        survey.biosphere,
    )

    if not survey.body.is_shielded:
        return "magnetic_shield"  # nothing you release stays without one
    if air.pressure_bar < WORKING_PRESSURE_BAR:
        return "atmosphere_processor"
    if climate.surface_temp_k < GROWING_BAND_K[0]:
        return "greenhouse_seeding"
    if climate.surface_temp_k > GROWING_BAND_K[1]:
        return "orbital_shade"  # overshot; reflect some of it back
    if not water.liquid_water:
        return "cometary_redirection"
    if life.stage in UNFINISHED_LIFE:
        return "ecosystem_seeding"
    if air.toxins():
        return "atmospheric_scrubbing"  # thick and poisonous is worse than thin
    if not air.is_breathable:
        return "oxygenation"
    return None


def next_step(survey) -> Project | None:
    """:func:`next_project`, resolved to the catalogue entry."""
    key = next_project(survey)
    return project(key) if key is not None else None


def is_finished(survey) -> bool:
    """Whether this world has nothing left worth doing to it."""
    return next_project(survey) is None
