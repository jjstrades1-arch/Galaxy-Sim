"""Writing a finished project back into the planet.

The rule this module exists to enforce: **terraforming edits the world, and the
world is then re-derived by the same functions that derived it at generation.**
There is no terraforming-specific habitability, no bonus stapled to the colony,
no second code path. A project changes pressure or albedo or the biosphere, and
then temperature, breathability, habitability and carrying capacity all fall out
of the changed physics exactly as they did the day the world was rolled.

That is worth the extra work for one reason: it means the survey never lies. A
world halfway through terraforming reads like a world that is naturally halfway
there, because as far as every other system is concerned, it is. And when a
project has an unintended consequence -- thickening the air of a hot world makes
it hotter, because greenhouse forcing scales with pressure -- the game did not
have to be taught that. It follows.
"""

from __future__ import annotations

from dataclasses import replace

from galaxysim.terraform.projects import (
    COOL,
    DELIVER_WATER,
    OXYGENATE,
    SCRUB_TOXINS,
    SEED_LIFE,
    SHIELD,
    THICKEN_ATMOSPHERE,
    WARM,
    Project,
)
from galaxysim.worldgen.biosphere import (
    CARBON_WATER,
    COMPLEX,
    MICROBIAL,
    SIMPLE,
    STAGES,
)
from galaxysim.worldgen.planet import (
    TOXIC_LIMITS,
    equilibrium_temperature,
    greenhouse_forcing,
)
from galaxysim.worldgen.survey import Survey, derive_habitability

#: Gas released by an atmosphere processor. Nitrogen because it is inert, common
#: in crustal volatiles, and the bulk of the only breathable atmosphere anyone
#: has ever measured.
FILLER_GAS = "N2"

#: Most of the starlight a world can be made to throw back. A shade is a swarm
#: of statites, not a lid: past this there is nothing left to shadow.
#:
#: The ceiling is load-bearing rather than cosmetic. A world whose greenhouse
#: forcing alone exceeds the growing band cannot be cooled to it by reflection
#: at any price, which makes it *unfinishable* -- see
#: :func:`galaxysim.terraform.plan.is_finishable`, which exists because the
#: ladder used to ask for another shade for ever instead of saying so.
MAX_ALBEDO = 0.85

#: A magnetic shield is machinery, not a dynamo, so it is recorded as a field
#: strong enough to deflect the stellar wind rather than as a molten core.
SHIELDED_FIELD_GAUSS = 0.35


def apply_project(survey: Survey, project: Project) -> Survey:
    """Return ``survey`` as it stands after one completed run of ``project``.

    Pure: it takes a survey and gives back a new one, so the caller decides when
    to persist and the same function can be used to *preview* what a project
    would do without committing to it.
    """
    changed = _apply_effect(survey, project)
    return _rederive(changed)


# ------------------------------------------------------------------ effects


def _apply_effect(survey: Survey, project: Project) -> Survey:
    effect, size = project.effect, project.magnitude

    if effect == SHIELD:
        return replace(
            survey, body=replace(survey.body, magnetic_field_gauss=SHIELDED_FIELD_GAUSS)
        )

    if effect == THICKEN_ATMOSPHERE:
        return replace(survey, atmosphere=_add_gas(survey, FILLER_GAS, size))

    if effect == OXYGENATE:
        return replace(survey, atmosphere=_add_gas(survey, "O2", size))

    if effect == SCRUB_TOXINS:
        return replace(survey, atmosphere=_scrub(survey, size))

    if effect == WARM:
        # Warming is done by trapping heat, so it is recorded as greenhouse
        # forcing rather than as a temperature. That matters: forcing persists
        # and interacts, where a temperature written directly would be silently
        # overwritten the next time anything re-derived the climate.
        return replace(
            survey,
            atmosphere=replace(
                survey.atmosphere, greenhouse_k=survey.atmosphere.greenhouse_k + size
            ),
        )

    if effect == COOL:
        # Cooling is done by reflecting light away, so the magnitude is albedo
        # and not degrees. How much colder that makes the surface is not this
        # function's to say: it depends on the star, the orbit and the air, and
        # it falls out of _rederive like everything else.
        albedo = min(MAX_ALBEDO, survey.atmosphere.albedo + size)
        return replace(survey, atmosphere=replace(survey.atmosphere, albedo=albedo))

    if effect == DELIVER_WATER:
        hydro = survey.hydrosphere
        ocean = min(0.95, hydro.ocean_fraction + size)
        return replace(
            survey,
            hydrosphere=replace(
                hydro,
                ocean_fraction=round(ocean, 4),
                mean_ocean_depth_km=max(hydro.mean_ocean_depth_km, 1.5),
            ),
        )

    if effect == SEED_LIFE:
        return replace(survey, biosphere=_advance_life(survey))

    raise ValueError(f"unknown terraforming effect {effect!r}")


def _add_gas(survey: Survey, gas: str, bar: float) -> object:
    """Add ``bar`` of a gas, renormalising the composition around it.

    Composition is mole fractions summing to one, so adding a partial pressure
    means recomputing every fraction against the new total. Doing it any other
    way -- bumping one fraction and leaving the rest -- would quietly change the
    amount of every other gas present, which is how an atmosphere ends up
    contradicting itself.
    """
    atmosphere = survey.atmosphere
    old_pressure = max(0.0, atmosphere.pressure_bar)
    partials = {g: old_pressure * f for g, f in atmosphere.composition.items()}
    partials[gas] = partials.get(gas, 0.0) + bar

    total = sum(partials.values())
    if total <= 0:
        return atmosphere
    return replace(
        atmosphere,
        pressure_bar=round(total, 6),
        composition={g: round(p / total, 6) for g, p in sorted(partials.items()) if p > 0},
    )


def _scrub(survey: Survey, fraction: float) -> object:
    """Remove a fraction of every gas over its toxic limit."""
    atmosphere = survey.atmosphere
    pressure = max(0.0, atmosphere.pressure_bar)
    partials = {g: pressure * f for g, f in atmosphere.composition.items()}

    for gas, limit in TOXIC_LIMITS.items():
        if partials.get(gas, 0.0) > limit:
            partials[gas] *= max(0.0, 1.0 - fraction)

    total = sum(partials.values())
    if total <= 0:
        return atmosphere
    return replace(
        atmosphere,
        pressure_bar=round(total, 6),
        composition={
            g: round(p / total, 6) for g, p in sorted(partials.items()) if p > 1e-12
        },
    )


def _advance_life(survey: Survey) -> object:
    """Move the biosphere one stage along, in compatible chemistry.

    Seeded life is *your* life, so it is carbon-water by construction -- which
    is the quiet payoff: a world you terraformed is a world you can farm, where
    a world with native life may be neither edible nor safe.
    """
    biosphere = survey.biosphere
    order = list(STAGES)
    # A sterile world starts at microbial: you are not seeding prebiotic soup,
    # you are releasing organisms.
    current = order.index(biosphere.stage) if biosphere.stage in order else 0
    target = max(order.index(MICROBIAL), min(order.index(COMPLEX), current + 1))

    stage = order[target]
    return replace(
        biosphere,
        stage=stage,
        biochemistry=CARBON_WATER,
        biomass_tonnes=max(biosphere.biomass_tonnes, 10.0 ** (9 + target)),
        # Photosynthesis begins once there is something to photosynthesise with.
        oxygenating=biosphere.oxygenating or target >= order.index(SIMPLE),
        # Life you introduced yourself carries nothing you have no immunity to.
        pathogen_hazard=min(biosphere.pathogen_hazard, 0.1),
    )


# ------------------------------------------------------------- re-derivation


def _rederive(survey: Survey) -> Survey:
    """Recompute everything that follows from the physical state.

    Same functions the generator uses. This is where a project's *unintended*
    consequences come from: thickening the air of a warm world raises its
    greenhouse forcing and makes it hotter, and nothing had to be taught that
    because the forcing was always a function of pressure and composition.
    """
    atmosphere = survey.atmosphere
    equilibrium = equilibrium_temperature(
        survey.star, survey.orbit.semi_major_axis_au, atmosphere.albedo
    )
    # Greenhouse is whatever the gases do, plus whatever has been deliberately
    # engineered into the sky and is being maintained.
    natural = greenhouse_forcing(atmosphere.pressure_bar, atmosphere.composition)
    engineered = max(0.0, atmosphere.greenhouse_k - _natural_at_generation(survey))
    surface = equilibrium + natural + engineered

    climate = replace(
        survey.climate,
        equilibrium_temp_k=round(equilibrium, 2),
        surface_temp_k=round(surface, 2),
    )
    atmosphere = replace(atmosphere, greenhouse_k=round(natural + engineered, 3))

    hydrosphere = _rederive_hydrosphere(survey, surface)
    updated = replace(
        survey,
        atmosphere=atmosphere,
        climate=climate,
        hydrosphere=hydrosphere,
    )
    return replace(
        updated,
        habitability=derive_habitability(
            atmosphere, climate, survey.body, hydrosphere, survey.biosphere
        ),
    )


def _natural_at_generation(survey: Survey) -> float:
    """What the gases alone would produce, for separating out engineered heat."""
    return greenhouse_forcing(
        survey.atmosphere.pressure_bar, survey.atmosphere.composition
    )


def _rederive_hydrosphere(survey: Survey, surface_temp_k: float) -> object:
    """Whether the water on this world is liquid, at the new temperature.

    A world can be given an ocean and then have it freeze, or be warmed until
    its ice caps melt. Both should follow from the temperature rather than from
    which project was run last.
    """
    from galaxysim.worldgen.planet import WATER_FREEZE_K

    hydro = survey.hydrosphere
    pressure = survey.atmosphere.pressure_bar
    # Liquid water needs to be above freezing and under enough pressure not to
    # sublimate straight to vapour. The triple point of water is 0.006 bar.
    liquid = surface_temp_k > WATER_FREEZE_K and pressure > 0.006 and hydro.ocean_fraction > 0
    ice = hydro.ice_fraction
    if liquid and surface_temp_k > WATER_FREEZE_K + 15.0:
        ice = round(max(0.0, ice * 0.5), 4)
    elif not liquid and hydro.ocean_fraction > 0:
        ice = round(min(1.0, ice + hydro.ocean_fraction * 0.5), 4)
    return replace(hydro, liquid_water=liquid, ice_fraction=ice)
