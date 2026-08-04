"""Power, and what it does to a colony that has not got enough.

Two families of property.

**Power is a rate against a rate.** Everything else a colony holds is a pile
that sits there until spent; electricity is generated and consumed in the same
instant, and a shortfall throttles rather than kills. The tests below check the
throttle exists, that it has a floor, and that it lifts when fuel arrives.

**What a world can power itself with is a fact about the world.** Solar reads
stellar flux, geothermal reads tectonic activity, and both come off promoted
columns rather than the survey document -- so a red dwarf's outer rocks are dark
and a volcanic world heats its own mines, and neither needed a rule.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.colony import energy
from galaxysim.colony.buildings import building_type
from galaxysim.colony.industry import cost_of_level
from galaxysim.colony.labor import INDUSTRY, balanced_allocation
from galaxysim.engine import intents
from galaxysim.engine.context import TickContext
from galaxysim.engine.rates import CADENCE_HOURLY, DEFAULT_RATES
from galaxysim.engine.resolvers.production import (
    _extraction_capacity,
    effective_habitability,
    effects_for,
    industry_capacity,
    industry_output,
)
from galaxysim.engine.tick import run_ticks
from galaxysim.materials import DEUTERIUM, FISSILES, HELIUM3
from galaxysim.model.base import open_session
from galaxysim.model.entities import Building, Colony, Event, Universe, World
from galaxysim.colony.labor import normalize
from tests.conftest import (
    OUTPOST_POPULATION,
    civ_by_name,
    give_deposits,
    home_colony,
    new_universe,
    rich_stockpile,
    take_manual_control,
)


# --- the physics of it, with no database in the way --------------------------


def test_a_dark_world_gets_nothing_from_collectors():
    """Three quarters of stars are red dwarfs and their worlds are dark.

    Solar is the cheapest route and the one with no fuel bill, so it has to be
    the one that most often does not work -- otherwise every colony builds
    panels and the other three routes are decoration.
    """
    bright = energy.solar_output(levels=10, stellar_flux=1.0)
    dim = energy.solar_output(levels=10, stellar_flux=0.02)

    assert bright > dim * 20
    assert energy.solar_output(levels=10, stellar_flux=0.0) == 0.0

    # And there is nothing more to collect past the reference: a world roasting
    # at three hundred times Earth's flux is limited by panel area, not sunlight.
    assert energy.solar_output(10, 300.0) == energy.solar_output(10, 1.0)


def test_a_dead_world_has_no_ground_heat():
    """Geothermal pays exactly where the ore is richest, and nowhere else.

    Tectonic activity is what concentrates a diffuse element into a seam, so the
    worlds worth mining are the worlds that can power the mine -- and the dead
    ones that are cheap to live on are the ones that must import their fuel.
    Both halves of that fall out of one number the generator already rolled.
    """
    assert energy.geothermal_output(levels=10, tectonic_activity=0.0) == 0.0
    live = energy.geothermal_output(levels=10, tectonic_activity=0.9)
    assert live > 0


def test_generation_is_linear_in_level_and_its_price_is_not():
    """Ten reactors make ten reactors' power. The tenth costs a hundred times.

    The square-root curve everywhere else in this game is right for a
    *multiplier* -- a level-100 mine raises a rate. Power is a quantity being
    produced rather than a rate being raised, so it is linear, and the
    diminishing return lives in the quadratic materials bill instead. That way
    the economics still discourage stacking one plant forever without the
    physics having to pretend.
    """
    assert energy.solar_output(20, 1.0) == pytest.approx(
        energy.solar_output(10, 1.0) * 2
    )

    spec = building_type("fusion_plant")
    first = sum(spec.cost.values())
    tenth = sum(cost_of_level(spec.cost, 10).values())
    assert tenth == pytest.approx(first * 100)


def test_fusion_fuel_is_worth_crossing_a_frontier_for():
    """The reason a barren regolith world is worth having.

    Helium-3 sits at twenty parts per billion and is strip-mined by the
    megatonne. It is only worth that because a tonne of it is worth something
    like seventy tonnes of fissiles, and this is where that pays off.
    """
    per_unit = energy.FUEL_PER_POWER_HOUR
    assert per_unit[FISSILES] > per_unit[DEUTERIUM] > per_unit[HELIUM3]
    assert per_unit[FISSILES] / per_unit[HELIUM3] > 50

    # And a colony burns the densest thing it has, which is what a colony would.
    draw = energy.fuel_draw(1000.0, 1.0, {FISSILES: 1e9, HELIUM3: 1e9})
    assert set(draw) == {HELIUM3}


def test_a_brownout_has_a_floor():
    """A colony that lost all power could never mine the fuel to restart.

    A constraint you cannot recover from is a trap rather than a constraint, so
    the throttle bottoms out well above zero and a stranded colony crawls
    instead of dying.
    """
    assert energy.satisfaction(generation=0.0, wanted=1000.0) == (
        energy.MINIMUM_SATISFACTION
    )
    assert energy.MINIMUM_SATISFACTION > 0.0

    # And a colony that wants nothing is not in a blackout, which matters for
    # every landing party with nobody yet assigned to industry.
    assert energy.satisfaction(generation=0.0, wanted=0.0) == 1.0


def test_demand_follows_output_rather_than_headcount():
    """The distinction the whole mechanic rests on.

    The free baseline scales with people. If demand did too, the two would grow
    at exactly the same rate and power would never bind on anybody, at any size,
    forever. Scaling demand to industrial *output* means a colony that develops
    its industries outgrows its grid, which is when it should have to think
    about one.
    """
    people = 1.0e9
    baseline = energy.baseline_output(people)

    idle = energy.demand(0.0, 0.0, people, effective_habitability=1.0)
    working = energy.demand(baseline * 4, 0.0, people, effective_habitability=1.0)

    assert idle < baseline, "a colony doing nothing is comfortably supplied"
    assert working > baseline, "and one running deep industry is not"


# --- and what it does to a running colony ------------------------------------


def _hourly_ctx(session, universe) -> TickContext:
    return TickContext.build(session, universe, CADENCE_HOURLY, DEFAULT_RATES)


def _starve_of_power(colony) -> None:
    """Strip a colony of everything it could generate with."""
    colony.world.stellar_flux = 0.0
    colony.world.tectonic_activity = 0.0
    for material in (FISSILES, DEUTERIUM, HELIUM3):
        colony.stockpile.pop(material, None)


def test_a_fuelled_plant_with_an_empty_bunker_generates_nothing(engine):
    """Two separate ways to be short of power, and this is the supply one.

    A reactor is capacity; fuel is whether it runs. A colony can have either
    without the other, and only the fuel half is something a freighter can fix
    -- which is what makes an unpowered world a place with a supply line rather
    than a place with a problem.
    """
    universe_id = new_universe(
        engine, seed=7301, civs=("Terrans",), seconds_per_tick=3600
    )

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        _starve_of_power(colony)
        session.flush()

        # Plenty of reactors, nothing to put in them.
        assert energy.fuelled_capacity(fission_levels=100, fusion_levels=0) > 0
        assert energy.power_from_fuel_available(dict(colony.stockpile), hours=1.0) == 0.0

        # One delivery and the same reactors are worth their rating.
        with_fuel = dict(colony.stockpile)
        with_fuel[FISSILES] = 1.0e6
        assert energy.power_from_fuel_available(with_fuel, hours=1.0) > 0.0


def test_a_colony_that_outgrows_its_grid_is_throttled_until_it_builds_one(engine):
    """The loop the whole mechanic exists for.

    Industry demand rises with what a colony can produce, and a grid does not
    rise with it unless somebody builds. So a developed world that keeps
    deepening its industries walks itself into a brownout, and the way out is a
    power station rather than a policy change -- which is the decision this
    exists to create.
    """
    universe_id = new_universe(
        engine, seed=7301, civs=("Terrans",), seconds_per_tick=3600
    )

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        civ = civ_by_name(session, universe_id, "Terrans")
        take_manual_control(session, civ)
        colony = home_colony(session, civ)
        intents.set_labor(session, colony, {INDUSTRY: 1.0})
        colony.stockpile = rich_stockpile(1e10)
        _starve_of_power(colony)
        session.flush()
        colony_id = colony.id

    run_ticks(engine, universe_id, 2)
    with open_session(engine) as session:
        dark = session.get(Colony, colony_id)
        starved = dark.power_satisfaction
        assert starved < 0.9, "no sun, no ground heat, and industry flat out"

        # Commission enough fusion capacity to cover the gap. Fusion because it
        # is the one route that works regardless of where the world sits.
        session.add(
            Building(
                colony_id=colony_id,
                kind="fusion_plant",
                level=400,
                work_remaining=0.0,
                completed_tick=0,
            )
        )
        stock = dict(dark.stockpile)
        stock[HELIUM3] = 1.0e9
        dark.stockpile = stock

    run_ticks(engine, universe_id, 2)
    with open_session(engine) as session:
        lit = session.get(Colony, colony_id)
        assert lit.power_satisfaction > starved + 0.1, (
            f"power went {starved:.2f} -> {lit.power_satisfaction:.2f} after the "
            "reactors came online; building a grid should light the place up"
        )


def test_a_brownout_is_two_events_rather_than_one_a_tick(engine):
    """The log is what an offline player reads, and it was mostly one line.

    A brownout is a condition that *lasts*, and this logged it every tick it
    lasted. Measured over a 60-day soak: 8,506 of 19,907 events -- 43% of
    everything that happened in the game -- were eight homeworlds each writing
    "still at 50% power" fourteen hundred times, burying the 141 terraform
    completions and 95 life-support failures underneath.

    What a player needs is the pair a blockade already gives them: the lights
    went out, and later they came back on.
    """
    universe_id = new_universe(
        engine, seed=7304, civs=("Terrans",), seconds_per_tick=3600
    )

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        take_manual_control(session, civ)
        colony = home_colony(session, civ)
        intents.set_labor(session, colony, {INDUSTRY: 1.0})
        colony.stockpile = rich_stockpile(1e10)
        _starve_of_power(colony)
        session.flush()
        colony_id = colony.id

    run_ticks(engine, universe_id, 20)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.power_satisfaction < 0.9, "the fixture needs a real, lasting brownout"
        shortfalls = session.scalars(
            select(Event).where(Event.kind == "power_shortfall")
        ).all()
        assert len(shortfalls) == 1, (
            f"twenty ticks of one continuous brownout produced {len(shortfalls)} "
            "events; a standing condition is not news"
        )

        # And the other edge: give it the grid it was missing.
        session.add(
            Building(
                colony_id=colony_id,
                kind="fusion_plant",
                level=800,
                work_remaining=0.0,
                completed_tick=0,
            )
        )
        stock = dict(colony.stockpile)
        stock[HELIUM3] = 1.0e12
        colony.stockpile = stock

    run_ticks(engine, universe_id, 10)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.power_satisfaction >= 0.999, "the fixture needs the lights back on"
        restored = session.scalars(
            select(Event).where(Event.kind == "power_restored")
        ).all()
        assert len(restored) == 1, (
            f"coming back to full power should be one event, not {len(restored)}"
        )


def test_a_brownout_throttles_everything_industry_pays_for(engine):
    """One number, four consequences.

    Refining, construction, shipbuilding and terraforming all come out of the
    industry pool, so throttling the pool throttles all four at once -- which is
    why a governor treats power as more urgent than whatever its policy asked
    for, and why this is checked on the pool rather than on any one of them.
    """
    universe_id = new_universe(
        engine, seed=7302, civs=("Terrans",), seconds_per_tick=3600
    )

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        civ = civ_by_name(session, universe_id, "Terrans")
        take_manual_control(session, civ)
        colony = home_colony(session, civ)
        intents.set_labor(session, colony, {INDUSTRY: 1.0})
        session.flush()

        ctx = _hourly_ctx(session, universe)
        full = industry_capacity(ctx, colony)

        colony.power_satisfaction = 0.4
        throttled = industry_output(_hourly_ctx(session, universe), colony)

        assert full > 0
        assert throttled == pytest.approx(full * 0.4, rel=1e-6)


def test_a_landing_party_never_thinks_about_electricity(engine):
    """Small colonies are supplied by the baseline, and that is deliberate.

    A new colony has a hundred things to worry about and a grid should not be
    one of them. What it cannot do without power stations is run continent-scale
    smelting -- which is a problem it acquires by succeeding, not one it starts
    with.
    """
    universe_id = new_universe(
        engine, seed=7303, civs=("Terrans",), seconds_per_tick=3600
    )

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        civ = civ_by_name(session, universe_id, "Terrans")
        world = session.scalars(
            select(World).where(World.colony == None).order_by(World.id)  # noqa: E711
        ).first()
        give_deposits(world, iron=0.02, silicon=0.02)
        outpost = Colony(
            world_id=world.id,
            civ_id=civ.id,
            name="Landing",
            population=OUTPOST_POPULATION,
            founded_tick=0,
            stockpile={},
            labor=balanced_allocation(),
        )
        session.add(outpost)
        session.flush()

        ctx = _hourly_ctx(session, universe)
        effects = effects_for(ctx, outpost)
        allocation = normalize(outpost.labor)
        wanted = energy.demand(
            industry_capacity(ctx, outpost),
            _extraction_capacity(ctx, outpost, allocation, effects),
            outpost.population,
            effective_habitability(outpost, effects),
        )
        assert not effects.generation, "it has built nothing"
        assert energy.baseline_output(outpost.population) > wanted, (
            "a fifty-thousand-person landing should be comfortably self-powered"
        )


def test_a_governor_builds_power_before_whatever_its_policy_wanted(engine):
    """Power multiplies everything, so it outranks everything.

    A colony at seventy percent power is losing thirty percent of every other
    thing it does. No amount of the mine the policy asked for is worth as much
    as the reactor that un-throttles the mine already standing, and a governor
    that could not see that would develop a colony into a permanent brownout.
    """
    universe_id = new_universe(
        engine, seed=7304, civs=("Terrans",), seconds_per_tick=3600
    )

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        intents.set_management(session, colony, governed=True, policy="extraction")
        colony.stockpile = rich_stockpile(1e12)
        # Take away what it generates with and demolish the grid it opened with,
        # so the only way back to full output is to build one.
        colony.world.stellar_flux = 0.0
        colony.world.tectonic_activity = 0.0
        for building in list(colony.buildings):
            if building_type(building.kind).generation:
                session.delete(building)
        colony_id = colony.id

    run_ticks(engine, universe_id, 6)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        ordered = [
            b.kind for b in colony.buildings if building_type(b.kind).generation
        ]
        assert ordered, (
            "an extraction-policy governor on a dark, cold world should still "
            "have reached for a power station first"
        )
        # And not a solar array on a world with no sun.
        assert "solar_array" not in ordered
