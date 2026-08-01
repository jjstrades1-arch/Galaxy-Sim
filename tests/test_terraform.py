"""Terraforming: changing a planet's real numbers.

The property that matters most here is not that terraforming works -- it is that
it works *through the world*. A project edits pressure, albedo, water or life,
and then habitability, breathability, capacity and agricultural quality are all
re-derived by exactly the functions that derived them at generation. Nothing
reads a terraforming flag.

Two things follow from that, and both are tested below: the survey never lies
about a half-terraformed world, and projects have consequences nobody wrote
down -- thickening the air of a warm world makes it hotter, because greenhouse
forcing has always been a function of pressure.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.colony.labor import INDUSTRY, balanced_allocation
from galaxysim.colony.population import capacity
from galaxysim.engine import intents
from galaxysim.engine.resolvers.production import SUPPLY_RANGE_LY
from galaxysim.engine.resolvers.terraform import unmet_requirements
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Colony, Event, Intent, IntentStatus, World
from galaxysim.terraform.apply import apply_project
from galaxysim.terraform.projects import (
    NEEDS_ATMOSPHERE,
    NEEDS_LIQUID_WATER,
    NEEDS_SHIELD,
    PROJECTS,
    project,
)
from galaxysim.worldgen.serialize import survey_from_json, survey_to_json
from tests.conftest import (
    civ_by_name,
    give_deposits,
    home_colony,
    new_universe,
    rich_stockpile,
    take_manual_control,
)


def _fresh(seed: int):
    """A universe of its own, for tests that compare two whole runs."""
    engine = create_engine_for("sqlite://")
    return engine, new_universe(
        engine, seed=seed, civs=("Terrans",), seconds_per_tick=3600
    )


def _cold_rock(session) -> World:
    """A dead world people could stand on: the classic terraforming target."""
    best = None
    for world in session.scalars(select(World).order_by(World.id)):
        if world.colony is not None:
            continue
        survey = survey_from_json(world.survey)
        if not (0.5 <= survey.body.gravity_g <= 1.6):
            continue
        if survey.climate.surface_temp_k >= 260:
            continue
        if best is None or survey.climate.surface_temp_k > best[1]:
            best = (world, survey.climate.surface_temp_k)
    assert best is not None, "the test universe has no cold rock in it"
    return best[0]


#: The surface temperature a terraformer is actually aiming at -- warm enough to
#: grow food in, cool enough to stand in.
GROWING_BAND_K = (283.0, 303.0)

#: Enough air to breathe and to hold heat, below which nothing else is worth
#: doing. Roughly Earth's, which is the only figure anyone has ever tested.
WORKING_PRESSURE_BAR = 0.7


def _next_project(survey) -> str | None:
    """The project this world needs next, chosen from what it currently is.

    Terraforming has to be driven by feedback rather than a recipe, because the
    projects interact. Greenhouse forcing scales with pressure, so thickening
    the air warms the world *as well as* pressurising it; scrubbing sulphur back
    out thins the air again and may put pressure below working. A fixed sequence
    that is right for one planet cooks the next one.

    So this reads the survey and picks, the way a player looking at the readout
    would -- and the loops that use it are therefore a statement about the
    physics converging, not about a list of eleven project names being correct.
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
    if life.stage in ("sterile", "prebiotic", "microbial"):
        return "ecosystem_seeding"
    if air.toxins():
        return "atmospheric_scrubbing"  # thick and poisonous is worse than thin
    if not air.is_breathable:
        return "oxygenation"
    return None


def _terraform(survey, limit: int = 40):
    """Run projects against a world until it is finished or stops converging."""
    for _ in range(limit):
        key = _next_project(survey)
        if key is None:
            return survey
        spec = project(key)
        assert not unmet_requirements(survey, spec), (
            f"{spec.name} was chosen for a world that cannot take it: "
            f"{unmet_requirements(survey, spec)}"
        )
        survey = apply_project(survey, spec)
    raise AssertionError(
        f"terraforming did not converge in {limit} projects; the world sits at "
        f"{survey.climate.surface_temp_k:.0f} K, {survey.atmosphere.pressure_bar:.2f} bar, "
        f"habitability {survey.habitability}"
    )


# --- the catalogue -----------------------------------------------------------


def test_the_catalogue_is_coherent():
    for key, spec in PROJECTS.items():
        assert spec.key == key
        assert spec.name and spec.description
        assert spec.cost and spec.work > 0
        assert spec.magnitude > 0
    with pytest.raises(KeyError):
        project("planet_cracker")


# What a project costs relative to everything else -- and how long it takes,
# which turned out to matter more -- is calibrated in ``tests/test_prices.py``
# against a measured reference economy rather than asserted as a ratio here.
# The version that lived at this spot claimed a project cost ten thousand times
# the dearest building, which was true only because buildings had been left at
# pre-rescale prices and cost thirty tonnes.


# --- preconditions are physics ------------------------------------------------


def test_preconditions_describe_the_planet_not_a_tech_tree(engine):
    """The order projects can run in falls out of what is physically possible."""
    new_universe(engine, seed=5150, civs=("Terrans",), system_count=24)

    with open_session(engine) as session:
        world = _cold_rock(session)
        survey = survey_from_json(world.survey)

        # Strip the field: now nothing that puts gas in the sky can start.
        from dataclasses import replace

        airless = replace(
            survey,
            body=replace(survey.body, magnetic_field_gauss=0.0),
            atmosphere=replace(survey.atmosphere, pressure_bar=0.0, composition={}),
        )
        assert NEEDS_SHIELD in unmet_requirements(airless, project("atmosphere_processor"))
        assert NEEDS_ATMOSPHERE in unmet_requirements(airless, project("greenhouse_seeding"))
        assert NEEDS_LIQUID_WATER in unmet_requirements(airless, project("ecosystem_seeding"))

        # A shield needs nothing -- it is always the first thing you can do.
        assert unmet_requirements(airless, project("magnetic_shield")) == ()


def test_warming_a_world_that_is_already_an_oven_is_allowed_and_stupid(engine):
    """Physics, not guard rails.

    Greenhouse forcing scales with pressure, so thickening the air of a hot
    world makes it hotter. Nothing had to be taught that and nothing prevents
    it: the game lets you make a bad decision and shows you the result.
    """
    new_universe(engine, seed=5150, civs=("Terrans",), system_count=24)

    with open_session(engine) as session:
        hot = None
        for world in session.scalars(select(World).order_by(World.id)):
            survey = survey_from_json(world.survey)
            if survey.climate.surface_temp_k > 700 and survey.atmosphere.pressure_bar > 1:
                hot = survey
                break
        assert hot is not None, "the test universe has no oven in it"

        after = apply_project(hot, project("atmosphere_processor"))
        assert after.climate.surface_temp_k > hot.climate.surface_temp_k
        assert after.habitability <= hot.habitability


# --- the transformation ------------------------------------------------------


def test_terraforming_turns_an_outpost_into_a_world(engine):
    """The payoff the whole tree exists for.

    A dead world's ceiling is what its habitats hold -- a few hundred thousand.
    Run the sequence and the ceiling becomes ``land x density x habitability``,
    which is billions. Four orders of magnitude, and the only thing in the game
    that produces it.
    """
    new_universe(engine, seed=5150, civs=("Terrans",), system_count=24)

    with open_session(engine) as session:
        world = _cold_rock(session)
        survey = survey_from_json(world.survey)
        before_habitability = survey.habitability
        before_capacity = survey.carrying_capacity

        survey = _terraform(survey)

    assert before_habitability < 0.05, "it started dead"
    assert survey.habitability > 0.5, "and finished liveable"
    assert survey.atmosphere.is_breathable, "with air people can breathe"
    assert survey.hydrosphere.liquid_water, "and water they can drink"
    assert survey.carrying_capacity > 1e9
    assert survey.carrying_capacity > max(before_capacity, 1.0) * 1_000


def test_the_ceiling_switches_regime_rather_than_merely_rising(engine):
    """Before: habitats. After: the planet.

    This is the specific thing that makes terraforming worth a civilization's
    entire surplus. It is not a bonus to an outpost -- it *stops the outpost
    being an outpost*.
    """
    new_universe(engine, seed=5150, civs=("Terrans",), system_count=24)

    with open_session(engine) as session:
        world = _cold_rock(session)
        survey = survey_from_json(world.survey)

        # As a dead world, habitats are the only ceiling and industry is what
        # raises it -- a lot of industry for very few people.
        world.habitability = survey.habitability
        world.carrying_capacity = survey.carrying_capacity
        outpost_ceiling = capacity(world, infrastructure=30.0)
        assert outpost_ceiling < 1e7

        survey = _terraform(survey)

        world.habitability = survey.habitability
        world.carrying_capacity = survey.carrying_capacity
        world_ceiling = capacity(world, infrastructure=30.0)

    assert world_ceiling / outpost_ceiling > 1_000


# --- through the engine -------------------------------------------------------


def test_a_project_runs_through_the_tick_loop_and_changes_the_stored_world(engine):
    """End to end: order it, feed it industry, watch the planet change."""
    universe_id = new_universe(
        engine, seed=5150, civs=("Terrans",), system_count=24, seconds_per_tick=3600
    )

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = rich_stockpile(1e12)

        # Strip the capital's own world of its field, so there is something to
        # fix on a colony that has the industry to pay for fixing it. Moving the
        # capital to a rock instead would work too, and then measure nothing:
        # the rock's land area caps its industry and the project never finishes.
        from dataclasses import replace

        survey = survey_from_json(home.world.survey)
        home.world.survey = survey_to_json(
            replace(survey, body=replace(survey.body, magnetic_field_gauss=0.0))
        )
        session.flush()
        colony_id, world_id = home.id, home.world_id
        assert not survey_from_json(home.world.survey).body.is_shielded

        intents.terraform(session, civ, colony_id, "magnetic_shield")

    run_ticks(engine, universe_id, 400)

    with open_session(engine) as session:
        world = session.get(World, world_id)
        assert survey_from_json(world.survey).body.is_shielded, (
            "the finished project should be written into the planet"
        )
        assert session.scalars(
            select(Event).where(Event.kind == "terraform_completed")
        ).all()


def _outpost_beside(session, civ, home, offset_ly: float):
    """Settle a dead world and park it ``offset_ly`` from the capital.

    Positions are moved rather than searched for, because what is under test is
    the distance rule and picking real systems at two chosen ranges out of a
    generated galaxy is a test about the galaxy.
    """
    # Somewhere other than the capital's own system, or moving the star moves
    # the capital with it and the distance under test is always zero.
    world = session.scalars(
        select(World)
        .where(World.colony == None, World.system_id != home.world.system_id)  # noqa: E711
        .order_by(World.id)
    ).first()
    world.habitability = 0.0
    give_deposits(world, iron=0.02, silicon=0.02)
    system = world.system
    system.x = home.world.system.x + offset_ly
    system.y, system.z = home.world.system.y, home.world.system.z

    colony = Colony(
        world_id=world.id,
        civ_id=civ.id,
        name="Anvil",
        population=6.0e4,
        founded_tick=0,
        stockpile=rich_stockpile(1e12),
        labor=balanced_allocation(),
        management_mode="manual",
    )
    session.add(colony)
    session.flush()
    return colony


def _work_left(session) -> float:
    intent = session.scalar(
        select(Intent).where(Intent.kind == "terraform").order_by(Intent.id)
    )
    return float(intent.payload.get("work_remaining", 0.0))


def test_a_neighbourhood_terraforms_faster_than_a_lone_outpost(engine):
    """Why an empire reshapes planets and a single colony cannot.

    The worlds worth terraforming are dead ones, a dead world caps at outpost
    scale, and an outpost produces a rounding error of industry -- so for as
    long as a project drew only on the colony standing on it, the entity doing
    the work was the one least able to do it, and the only way to get better at
    the job was to finish it. Nothing could ever start.

    Drawing on every colony within supply range breaks that, and the shape of
    what replaces it is the point: a project goes faster because there is a
    developed world *near it*. Distance is what decides, not the size of the
    empire on paper.
    """

    def progress(offset_ly: float) -> float:
        engine_, universe_id = _fresh(seed=6120)
        with open_session(engine_) as session:
            civ = civ_by_name(session, universe_id, "Terrans")
            take_manual_control(session, civ)
            home = home_colony(session, civ)
            home.stockpile = rich_stockpile(1e12)
            intents.set_labor(session, home, {INDUSTRY: 1.0})

            outpost = _outpost_beside(session, civ, home, offset_ly)
            intents.terraform(session, civ, outpost.id, "magnetic_shield")

        run_ticks(engine_, universe_id, 3)
        with open_session(engine_) as session:
            spec = project("magnetic_shield")
            return spec.work - _work_left(session)

    near = progress(SUPPLY_RANGE_LY * 0.5)
    far = progress(SUPPLY_RANGE_LY * 4.0)

    assert near > far * 10, (
        f"a project {SUPPLY_RANGE_LY * 0.5:.0f} ly from the capital advanced "
        f"{near:,.0f} against {far:,.0f} at four times the range; the capital's "
        "industry should reach one and not the other"
    )
    assert far > 0, "the outpost still does its own share, however small"


def test_a_project_that_cannot_start_says_which_physical_fact_is_missing(engine):
    universe_id = new_universe(
        engine, seed=5150, civs=("Terrans",), system_count=24, seconds_per_tick=3600
    )

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        # The capital has air and a field; what it has not got is an ocean.
        # So the failure it should report is the water, specifically.
        from dataclasses import replace

        survey = survey_from_json(home.world.survey)
        home.world.survey = survey_to_json(
            replace(
                survey,
                hydrosphere=replace(survey.hydrosphere, liquid_water=False, ocean_fraction=0.0),
            )
        )
        session.flush()
        intents.terraform(session, civ, home.id, "ecosystem_seeding")

    run_ticks(engine, universe_id, 2)

    with open_session(engine) as session:
        order = session.scalar(
            select(intents.Intent).where(intents.Intent.kind == "terraform")
        )
        assert order.status == IntentStatus.FAILED.value
        assert "liquid water" in order.result


def test_a_project_is_charged_up_front(engine):
    universe_id = new_universe(
        engine, seed=5150, civs=("Terrans",), system_count=24, seconds_per_tick=3600
    )
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = {}
        intents.terraform(session, civ, home.id, "magnetic_shield")
        colony_id = home.id

    run_ticks(engine, universe_id, 2)

    with open_session(engine) as session:
        order = session.scalar(
            select(intents.Intent).where(intents.Intent.kind == "terraform")
        )
        assert order.status == IntentStatus.QUEUED.value
        assert "insufficient" in order.result
        assert session.get(Colony, colony_id).world.habitability >= 0.0
