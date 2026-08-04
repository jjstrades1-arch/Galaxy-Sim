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

from galaxysim.terraform.apply import apply_project
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


#: Rungs to walk before calling a world unfinishable. A world that can be
#: finished takes a median of fifteen projects and never more than twenty-three,
#: measured across two thousand generated worlds, so this is generous and still
#: bounded -- and it is the bound that makes :func:`is_finishable` safe to call
#: on every world in the galaxy.
LADDER_LIMIT = 60


def next_project(survey) -> str | None:
    """The project this world needs next, or ``None`` if nothing more can be done.

    "Nothing more" covers two cases that are worth telling apart in prose and
    worth treating identically everywhere else. A world can be *finished* --
    shielded, breathable, watered and alive. Or it can be **as far as anybody can
    take it**: the ladder still wants something, and the something no longer
    changes anything.

    That second case is not hypothetical, it is two thirds of the galaxy. An
    orbital shade cools by reflecting light, and albedo saturates; a Venus at
    690 K with its albedo already at the cap is not going to be parasoled down to
    a growing band, and no amount of money changes that. What
    :func:`_wanted` alone said was "it needs another shade", for ever -- so a
    civilization would sink 43.5 million work and sixty million tonnes into a
    project that provably could not alter the survey, and then do it again.

    So the answer is checked against the physics before it is given: apply the
    rung, and if the world comes back identical, the road ends here. That is
    cheap because :func:`galaxysim.terraform.apply.apply_project` is pure and
    exists precisely to preview a project without committing to it, and it needs
    no caller to learn anything -- every reader of this function already treats
    ``None`` as "nothing to do".
    """
    key = _wanted(survey)
    if key is None:
        return None
    return key if apply_project(survey, project(key)) != survey else None


def campaign(survey) -> tuple[list[Project], object]:
    """Every project this world still needs, in order, and how it ends up.

    Walks the ladder to its end against a copy of the world. Terraforming is a
    pure function of the survey, so this is the real sequence rather than an
    estimate -- the projects that would actually run, in the order they would
    actually run, with every interaction they actually have. The returned survey
    is the world as it would then be, which is what makes "is this worth a
    civilization's surplus" answerable with numbers instead of prose.

    Whether the walk *ends* somewhere liveable is :func:`is_finishable`; this is
    the same journey, reported rather than judged.
    """
    steps: list[Project] = []
    for _ in range(LADDER_LIMIT):
        key = next_project(survey)
        if key is None:
            return steps, survey
        step = project(key)
        steps.append(step)
        survey = apply_project(survey, step)
    return steps, survey


def is_finishable(survey) -> bool:
    """Whether this world could ever be made somewhere people live.

    Worth knowing *before* committing, which is the whole point. Stopping at a
    dead rung stops the infinite sink, but a hopeless world still absorbs nine
    real projects on the way to saturating its albedo, and every one of them
    genuinely changes the survey. Neither a player nor the AI had any way to see
    that coming, and both spent accordingly.
    """
    _, ended = campaign(survey)
    return _wanted(ended) is None


def _wanted(survey) -> str | None:
    """What this world still needs, ignoring whether it can be given.

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
    """Whether this world is done -- genuinely done, not merely stuck.

    Distinct from ``next_project(survey) is None``, which is also true of a
    world nobody can take any further. This asks the ladder what it still wants
    and answers ``False`` if it wants anything at all.
    """
    return _wanted(survey) is None
