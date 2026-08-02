"""Changing a planet.

This is what a mature civilization's surplus is *for*. Everything else the
economy produces eventually piles up: a developed world mines faster than it
refines, refines faster than it builds, and once its industries are deep the
next level costs more than it returns. Terraforming is the sink at the end of
that chain, and it is the only one that pays back in something other than more
of the same.

**A project changes the planet's real numbers**, not a modifier stapled to the
colony. It rewrites the stored survey -- pressure, composition, albedo,
temperature, hydrosphere, magnetic field, biosphere -- and then habitability and
carrying capacity are re-derived from the changed world by exactly the functions
that derived them at generation. There is no terraforming-specific habitability
path, which is the property that keeps the survey honest: after a century of
work the readout says what the world *is*, and it says it the same way it always
did.

The payoff is the outpost-to-world transition. A dead world's ceiling is what
its habitats hold -- a few hundred thousand people. Lift habitability past the
liveable threshold and the ceiling becomes ``land x 53 x habitability``, which
on an ordinary rock is billions. That is a four-order-of-magnitude change from
one sequence of projects, and it is why reshaping a planet is worth a
civilization's entire surplus rather than a line item.

Projects are deliberately *slow and specific*. Each states the physical
precondition it needs, so the order they can be run in falls out of the physics
rather than a tech tree: there is no point seeding an ecosystem on a world that
cannot hold air, and no point releasing an atmosphere on a world with no
magnetic field to keep it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from galaxysim.materials.catalogue import (
    ALLOYS,
    CERAMICS,
    CONSTRUCTION,
    ELECTRONICS,
    FERTILISER,
    FISSILES,
    FUEL,
    POLYMERS,
    STEEL,
    WATER_ICE,
)

#: Effect keys. What a project actually does to the stored world, applied by
#: :mod:`galaxysim.terraform.apply`.
THICKEN_ATMOSPHERE = "thicken_atmosphere"
SCRUB_TOXINS = "scrub_toxins"
OXYGENATE = "oxygenate"
WARM = "warm"
COOL = "cool"
SHIELD = "shield"
DELIVER_WATER = "deliver_water"
SEED_LIFE = "seed_life"


@dataclass(frozen=True, slots=True)
class Project:
    """One engineering programme against a planet."""

    key: str
    name: str
    description: str
    #: What it does. Read by :mod:`galaxysim.terraform.apply`.
    effect: str
    #: Materials consumed at the colony running it, when work begins.
    cost: dict[str, float]
    #: Industry-work to finish. Enormous next to a building, and calibrated
    #: against the *sequence* rather than the project: turning a cold rock into
    #: somewhere people can breathe takes about fourteen of these, so a single
    #: one is a day or two of a developed capital's whole construction capacity
    #: and the campaign is roughly three weeks.
    #:
    #: That is for a civilization with one developed world. Projects pool
    #: construction across every colony within supply range
    #: (:mod:`galaxysim.engine.resolvers.terraform`), so an empire with a
    #: developed neighbourhood around the target does the same campaign in a
    #: day or two -- which is the largest single reward for having expanded, and
    #: the reason planet-scale engineering is something you grow into rather
    #: than something you start with.
    work: float
    #: How much of the effect one completed run delivers. Units depend on the
    #: effect -- bar, kelvin, fraction -- and each is documented below.
    magnitude: float = 0.0
    #: Physical facts that must already be true. Stated as plain strings so the
    #: failure a player sees explains the physics rather than citing a rule.
    requires: tuple[str, ...] = field(default_factory=tuple)


#: Preconditions, by the name used in :attr:`Project.requires`.
NEEDS_SHIELD = "a magnetic field to hold an atmosphere down"
NEEDS_ATMOSPHERE = "an atmosphere to work on"
NEEDS_LIQUID_WATER = "liquid water on the surface"
NEEDS_BREATHABLE = "air its colonists could already breathe"
NEEDS_TOLERABLE_TEMPERATURE = "a surface temperature life could survive"


PROJECTS: dict[str, Project] = {
    p.key: p
    for p in (
        Project(
            key="magnetic_shield",
            name="Magnetic Shield",
            description="A superconducting loop at the L1 point, or a driven "
            "current in the crust. Deflects the stellar wind that would "
            "otherwise strip away anything you release into the sky. "
            "Nothing else is worth starting until this stands.",
            effect=SHIELD,
            # No fissiles, deliberately, and it is the only project here without
            # them. A shield is a superconducting loop -- an enormous amount of
            # wire and switchgear, not a reactor. It also happens to be the
            # project that gates every other one, and requiring a rare material
            # for the first step meant a civilization that had not yet found
            # uranium could not begin terraforming at all: not slowly, not
            # expensively, but never. Fissiles still gate cometary redirection
            # and oxygenation, which genuinely are nuclear work.
            cost={STEEL: 4e+07, ALLOYS: 2e+07, ELECTRONICS: 1.2e+07},
            work=5.2e+07,
            magnitude=1.0,
        ),
        Project(
            key="atmosphere_processor",
            name="Atmosphere Processor",
            description="Continent-sized plants baking volatiles out of the "
            "regolith and venting them. Raises surface pressure, which is the "
            "precondition for every kind of weather.",
            effect=THICKEN_ATMOSPHERE,
            cost={STEEL: 3e+07, CERAMICS: 1.5e+07, POLYMERS: 8e+06, FUEL: 1e+07},
            work=3.9e+07,
            #: Bar of nitrogen added per run.
            magnitude=0.25,
            requires=(NEEDS_SHIELD,),
        ),
        Project(
            key="greenhouse_seeding",
            name="Greenhouse Seeding",
            description="Halocarbon factories and methane release. Traps heat, "
            "which is how you make a cold world habitable rather than merely "
            "pressurised.",
            effect=WARM,
            cost={CONSTRUCTION: 2e+07, POLYMERS: 1.5e+07, FUEL: 1.2e+07},
            work=2.6e+07,
            #: Kelvin added to the surface per run.
            magnitude=18.0,
            requires=(NEEDS_ATMOSPHERE,),
        ),
        Project(
            key="orbital_shade",
            name="Orbital Shade",
            description="A statite swarm at the sunward Lagrange point, "
            "throwing a fraction of the star's light back. The only thing that "
            "helps a world that is too hot rather than too cold.",
            effect=COOL,
            cost={ALLOYS: 3.5e+07, ELECTRONICS: 1.5e+07, CERAMICS: 1e+07},
            work=4.35e+07,
            #: Kelvin removed from the surface per run.
            magnitude=20.0,
        ),
        Project(
            key="cometary_redirection",
            name="Cometary Redirection",
            description="Nudge ice out of the outer system and let it fall. "
            "Slow, violent, and the only way to put an ocean on a world that "
            "never had one.",
            effect=DELIVER_WATER,
            cost={FUEL: 4e+07, FISSILES: 1e+06, ALLOYS: 1e+07, WATER_ICE: 5e+06},
            work=6.1e+07,
            #: Fraction of the surface covered in water per run.
            magnitude=0.18,
            requires=(NEEDS_ATMOSPHERE, NEEDS_TOLERABLE_TEMPERATURE),
        ),
        Project(
            key="atmospheric_scrubbing",
            name="Atmospheric Scrubbing",
            description="Fixing sulphur and ammonia out of the air and burying "
            "them. A thick poisonous atmosphere is further from breathable than "
            "no atmosphere at all, and this is what closes that gap.",
            effect=SCRUB_TOXINS,
            cost={CERAMICS: 2.5e+07, ELECTRONICS: 1e+07, CONSTRUCTION: 1.5e+07},
            work=3.5e+07,
            #: Fraction of each toxin removed per run.
            magnitude=0.7,
            requires=(NEEDS_ATMOSPHERE,),
        ),
        Project(
            key="ecosystem_seeding",
            name="Ecosystem Seeding",
            description="Cyanobacteria first, then everything that eats them. "
            "The slowest project and the only one that keeps working after you "
            "stop paying: a biosphere oxygenates its own sky.",
            effect=SEED_LIFE,
            cost={FERTILISER: 3e+07, POLYMERS: 1e+07, CONSTRUCTION: 2e+07},
            work=7.8e+07,
            #: Advances the biosphere one stage per run.
            magnitude=1.0,
            requires=(NEEDS_ATMOSPHERE, NEEDS_LIQUID_WATER, NEEDS_TOLERABLE_TEMPERATURE),
        ),
        Project(
            key="oxygenation",
            name="Oxygenation Plant",
            description="Electrolysis at planetary scale, for a world whose "
            "biosphere will not do it in time. Expensive, and the last step "
            "before people can walk outside.",
            effect=OXYGENATE,
            cost={ELECTRONICS: 2.5e+07, FISSILES: 2e+06, CERAMICS: 2e+07, ALLOYS: 1.5e+07},
            work=7.0e+07,
            #: Bar of O2 added per run.
            magnitude=0.08,
            requires=(NEEDS_ATMOSPHERE, NEEDS_SHIELD),
        ),
    )
}


def project(key: str) -> Project:
    """Look up a project, with a helpful error for a typo."""
    try:
        return PROJECTS[key]
    except KeyError:
        raise KeyError(
            f"unknown terraforming project {key!r}; known: {', '.join(sorted(PROJECTS))}"
        ) from None
