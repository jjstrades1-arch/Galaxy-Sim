"""Pacing: tick rate is granularity, not speed.

The design commitment these tests defend: the cadence controls how often the
world updates and *nothing else*. Running at five-minute ticks rather than
hourly gives a player a finer-grained history, not a faster civilization.

The way that breaks in practice is someone writing a rate "per tick" instead of
per hour -- it looks harmless, it passes every other test, and it silently makes
the game ten times faster at a finer cadence. These tests catch that.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.colony.labor import INDUSTRY, balanced_allocation
from galaxysim.colony.population import capacity
from galaxysim.engine import intents
from galaxysim.materials import HELIUM3, IRON, MATERIALS, STEEL
from galaxysim.engine.rates import CADENCE_FIVE_MINUTE, CADENCE_HOURLY, DEFAULT_RATES, Cadence
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Civ, Colony, Event, Fleet, Universe
from galaxysim.model.entities import World
from tests.conftest import (
    civ_by_name,
    give_deposits,
    held,
    home_colony,
    new_universe,
)

SIMULATED_HOURS = 48


def _run_for_hours(seconds_per_tick: int, hours: int, seed: int = 777) -> dict[str, float]:
    """Play the same opening for ``hours`` of simulated time at a given cadence."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=seed, seconds_per_tick=seconds_per_tick, civs=("Terrans",)
    )

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        intents.research(session, terrans)

    cadence = Cadence(seconds_per_tick)
    run_ticks(engine, universe_id, cadence.ticks_for_hours(hours))

    with open_session(engine) as session:
        civ = session.scalar(select(Civ).where(Civ.universe_id == universe_id).order_by(Civ.id))
        assert civ is not None
        colony = session.scalar(select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id))
        assert colony is not None
        return {
            "population": colony.population,
            "fullness": colony.population
            / capacity(colony.world, colony.infrastructure, colony.world.habitability),
            "steel": held(session, civ, STEEL),
            "research_invested": civ.research_invested,
            "techs_known": float(civ.techs_known),
        }


def test_growth_is_equivalent_across_cadences():
    """Two days of simulated time is two days of growth at any cadence.

    Tolerance is loose because rates compound at different granularities and
    research steps land on whole ticks. The property under test is that the
    numbers are the *same size*, not identical -- a per-tick rate leaking in
    would show up as a 12x gap, not a 2% one.
    """
    fine = _run_for_hours(CADENCE_FIVE_MINUTE.seconds_per_tick, SIMULATED_HOURS)
    coarse = _run_for_hours(CADENCE_HOURLY.seconds_per_tick, SIMULATED_HOURS)

    for key in ("population", "steel", "research_invested"):
        assert fine[key] == pytest.approx(coarse[key], rel=0.05), (
            f"{key} diverged across cadences: {fine[key]} vs {coarse[key]}. "
            "Something is almost certainly authored per tick instead of per hour."
        )


def _power_after_hours(seconds_per_tick: int, hours: int, seed: int = 7301) -> float:
    """How much power a hard-pressed capital gets, after ``hours`` at a cadence.

    Deliberately not the fixture the test above uses. A *fresh* homeworld is
    comfortably inside its grid and reads 1.000 at every cadence, which is
    exactly why the existing pace guard never noticed that power was cadence-
    dependent. This one puts the whole workforce in the smelters and takes away
    the sun and the ground heat, so the colony is genuinely short and the
    arithmetic has something to disagree about.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=seed, civs=("Terrans",), seconds_per_tick=seconds_per_tick
    )
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        intents.set_management(session, colony, governed=False)
        intents.set_labor(session, colony, {INDUSTRY: 1.0})
        colony.world.stellar_flux = 0.0
        colony.world.tectonic_activity = 0.0
        colony.stockpile = {IRON: 1e12, HELIUM3: 1e12}
        colony_id = colony.id

    run_ticks(engine, universe_id, Cadence(seconds_per_tick).ticks_for_hours(hours))

    with open_session(engine) as session:
        return session.get(Colony, colony_id).power_satisfaction


def test_power_does_not_depend_on_the_tick_rate():
    """The rule this module is named for, in the one system that broke it.

    ``_run_power`` compared per-*tick* industrial demand and baseline generation
    against per-*hour* life support, sunlight, ground heat and reactor capacity.
    At the hourly cadence everything in this project runs at, those coincide and
    the mixture is invisible; away from it the same world was at 0.496 power in
    an hourly universe, 1.000 at fifteen minutes and 0.415 at five.

    Power multiplies industry, extraction, refining, construction, shipbuilding
    and terraforming, so a cadence-dependent brownout makes the cadence set the
    pace of everything -- which is the precise thing this file exists to forbid.
    """
    hourly = _power_after_hours(CADENCE_HOURLY.seconds_per_tick, SIMULATED_HOURS)
    quarter = _power_after_hours(900, SIMULATED_HOURS)
    fine = _power_after_hours(CADENCE_FIVE_MINUTE.seconds_per_tick, SIMULATED_HOURS)

    assert hourly < 0.99, (
        f"the fixture has to actually brown out or this proves nothing (got {hourly})"
    )
    for label, value in (("15-minute", quarter), ("5-minute", fine)):
        assert value == pytest.approx(hourly, rel=0.02), (
            f"power at a {label} cadence is {value:.3f} but {hourly:.3f} hourly -- "
            "something in the power arithmetic is authored per tick instead of "
            "per hour"
        )


def test_cadence_converts_per_hour_rates():
    """The one place ticks are allowed to enter the arithmetic."""
    assert CADENCE_FIVE_MINUTE.per_tick(12.0) == pytest.approx(1.0)
    assert CADENCE_HOURLY.per_tick(12.0) == pytest.approx(12.0)
    assert CADENCE_FIVE_MINUTE.ticks_per_hour == 12.0


def test_ticks_for_hours_rounds_up():
    """A job needing part of a tick still occupies a whole one."""
    cadence = Cadence(300)
    assert cadence.ticks_for_hours(1.0) == 12
    assert cadence.ticks_for_hours(0.01) == 1  # 36 seconds -> one tick
    assert cadence.ticks_for_hours(0.0) == 0


def test_growth_is_slow_enough_to_take_real_time():
    """No get-big-quick path: a day of play must not finish the game.

    This is a floor, not a balance pass -- the real tuning happens in the pacing
    soak. It exists so that a well-meaning rate change that makes early growth
    ten times faster fails here instead of shipping.
    """
    after_one_day = _run_for_hours(CADENCE_FIVE_MINUTE.seconds_per_tick, 24)

    # The capital opens nearly full, so a day should barely move it. Measured
    # against capacity rather than an absolute headcount -- the number is in the
    # billions now and depends on which world the seed produced.
    assert after_one_day["fullness"] < 1.02
    # And research should be a handful of steps in, not dozens.
    assert after_one_day["techs_known"] < 12.0


def test_a_homeworld_is_already_full():
    """The opening position's whole argument.

    A species with lightspeed travel is not a landing party -- its homeworld has
    billions of people on it and is nearly at what the planet can hold. So it
    barely grows, and essentially all growth has to come from expanding. If this
    ever loosens, the capital becomes a thing you develop instead of a thing you
    launch from, and the game stops being about expansion.
    """
    # Hourly ticks: this measures four *weeks*, and at five-minute resolution
    # that would be eight thousand ticks to say something the cadence cannot
    # affect. Pace-invariance is proven above, so the coarse clock is free.
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=7788, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        assert colony.population > 1e8, "a homeworld holds billions, not a landing party"
        opening = colony.population
        ceiling = capacity(colony.world, colony.infrastructure, colony.world.habitability)
        assert opening / ceiling > 0.8, "it should open nearly full"
        colony_id = colony.id

    # Four weeks. A frontier colony would multiply many thousandfold in this.
    run_ticks(engine, universe_id, 672)  # hourly

    with open_session(engine) as session:
        grown = session.get(Colony, colony_id).population
        assert grown / opening < 1.2, (
            f"the capital grew {grown / opening:.2f}x in four weeks; it is supposed to be "
            "full already, so growth has to come from expanding"
        )


def test_research_cost_is_superlinear():
    """The curve that stops a civ from snowballing down one lineage.

    Checked for *accelerating* cost, not just increasing cost -- linear growth in
    cost would still let a large civ compound indefinitely.
    """
    rates = DEFAULT_RATES

    research_steps = [rates.research_cost(depth) for depth in range(0, 40)]
    research_deltas = [b - a for a, b in zip(research_steps, research_steps[1:])]
    assert all(b > a for a, b in zip(research_deltas, research_deltas[1:]))


def test_nothing_artificial_slows_a_wide_empire():
    """The counterpart, and the one that used to fail by design.

    There was a superlinear ``colony_overhead`` drag here that taxed a civ for
    holding colonies. It is gone: expansion *should* get easier as you grow,
    because that is the reward for growing. What slows a large empire is real --
    distance, supply lines, worlds that cost more to hold than they yield.

    Two identical colonies must therefore produce exactly twice what one does.
    """
    assert not hasattr(DEFAULT_RATES, "colony_overhead"), (
        "the artificial anti-expansion brake must stay deleted"
    )

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=7789, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = {}
        twins = [_twin_of(session, civ, home) for _ in range(3)]
        ids = (home.id, [t.id for t in twins])

    run_ticks(engine, universe_id, 24)  # hourly

    with open_session(engine) as session:
        home_id, twin_ids = ids
        first = session.get(Colony, twin_ids[0]).stockpile.get(IRON, 0.0)
        assert first > 0
        for other in twin_ids[1:]:
            assert session.get(Colony, other).stockpile.get(IRON, 0.0) == pytest.approx(
                first, rel=1e-6
            ), "the fourth colony must produce exactly what the second does"


def test_fleet_upkeep_is_charged_and_unpaid_fleets_desert():
    """A navy is a standing bill, not a one-time purchase.

    Without upkeep, hoarding ships is strictly correct and military size stops
    being a decision.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=5150, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        # A navy far beyond what one colony can support, and a bare stockpile at
        # the only colony that could supply it.
        fleet.strength = 500.0
        home_colony(session, civ).stockpile = {}
        fleet_id, strength_before = fleet.id, fleet.strength

    run_ticks(engine, universe_id, 24)

    with open_session(engine) as session:
        fleet = session.get(Fleet, fleet_id)
        assert fleet.strength < strength_before, "unpaid ships should desert"
        assert session.scalars(
            select(Event).where(Event.kind == "upkeep_shortfall")
        ).all()


def test_solvent_civ_keeps_its_fleet():
    """The counterpart: upkeep must not bleed a civ that can afford it."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=5151, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        home_colony(session, civ).stockpile = {key: 1e6 for key in MATERIALS}
        fleet_id, strength_before = fleet.id, fleet.strength

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        assert session.get(Fleet, fleet_id).strength == pytest.approx(strength_before)


def test_a_missed_payment_is_recorded_as_a_flow():
    """Not "what is banked" -- "did it keep up".

    A civ can hold three days of fuel, be draining steadily, keep buying hulls
    on the strength of the balance, and find out it overreached when ships start
    deserting. The stock says nothing about whether the bill is being met.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=5152, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        fleet.strength = 500.0
        home_colony(session, civ).stockpile = {}
        civ_id = civ.id

    run_ticks(engine, universe_id, 1)
    with open_session(engine) as session:
        assert session.get(Civ, civ_id).upkeep_paid < 1.0

    # Pay it, and the flag clears on the next tick it is charged.
    with open_session(engine) as session:
        civ = session.get(Civ, civ_id)
        home_colony(session, civ).stockpile = {key: 1e12 for key in MATERIALS}

    run_ticks(engine, universe_id, 1)
    with open_session(engine) as session:
        assert session.get(Civ, civ_id).upkeep_paid == pytest.approx(1.0)


def test_a_civ_that_missed_a_payment_does_not_buy_another_hull():
    """The AI's brake, asked as a flow question rather than a stock one."""
    from galaxysim.ai.simple import _Turn, _can_carry_more_upkeep

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=5153, civs=("Terrans",))

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        civ = civ_by_name(session, universe_id, "Terrans")
        colonies = [home_colony(session, civ)]
        colonies[0].stockpile = {key: 1e12 for key in MATERIALS}
        fleets = list(session.scalars(select(Fleet).where(Fleet.civ_id == civ.id)))
        # The reserve a civ keeps is part of how well it plays, so the check
        # reads it off the opponent's doctrine rather than a module constant.
        turn = _Turn(session, universe, civ)

        # Warehouses overflowing: the stock question says yes.
        civ.upkeep_paid = 1.0
        assert _can_carry_more_upkeep(turn, colonies, fleets, 2.0)

        # Same warehouses, but last tick's bill went unpaid. Something is wrong
        # with where the materials are rather than how many there are -- upkeep
        # is charged from the colonies near each fleet -- and buying another
        # hull cannot be the answer to it.
        civ.upkeep_paid = 0.8
        assert not _can_carry_more_upkeep(turn, colonies, fleets, 2.0)


def test_decommissioning_returns_materials_and_stops_the_bill():
    """Upkeep stops being a one-way ratchet.

    Until a hull could be broken up, the only way to stop paying for a ship with
    no purpose was to let its crew desert -- which returns nothing and is not a
    decision.
    """
    from galaxysim.colony.labor import LIFE_SUPPORT
    from galaxysim.materials import ELECTRONICS, FLEET_COST_PER_STRENGTH, SALVAGE_FRACTION
    from tests.conftest import take_manual_control

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=5154, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        take_manual_control(session, civ)
        home = home_colony(session, civ)
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        # Everyone on life support, so nothing is mined, refined or built while
        # the scrapping happens and the salvage is the only thing that moves.
        intents.set_labor(session, home, {LIFE_SUPPORT: 1.0})
        home.stockpile = {key: 1e9 for key in MATERIALS}
        home.stockpile[STEEL] = 0.0
        home.stockpile[ELECTRONICS] = 0.0
        # The fleet is parked over the homeworld, which is where it was built.
        fleet.x, fleet.y, fleet.z = (
            home.world.system.x,
            home.world.system.y,
            home.world.system.z,
        )
        intents.decommission_fleet(session, civ, fleet.id)
        fleet_id, home_id, strength = fleet.id, home.id, fleet.strength

    run_ticks(engine, universe_id, 1)

    with open_session(engine) as session:
        assert session.get(Fleet, fleet_id) is None, "the hull should be gone"
        home = session.get(Colony, home_id)
        for resource in (STEEL, ELECTRONICS):
            expected = FLEET_COST_PER_STRENGTH[resource] * strength * SALVAGE_FRACTION
            assert home.stockpile[resource] == pytest.approx(expected), (
                f"{resource} should have come back as salvage"
            )
        assert session.scalars(
            select(Event).where(Event.kind == "fleet_decommissioned")
        ).all()

    # And with nothing left flying there is no bill to miss.
    run_ticks(engine, universe_id, 24)
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        assert civ.upkeep_paid == pytest.approx(1.0)
        assert not session.scalars(
            select(Event).where(Event.kind == "upkeep_shortfall")
        ).all()


def test_universe_cadence_is_per_universe():
    """A solo game and the shared universe can tick at different resolutions."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seconds_per_tick=3600, civs=("Solo",))
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        assert universe is not None
        assert Cadence(universe.seconds_per_tick).ticks_per_hour == 1.0


def _twin_of(session, civ, home) -> Colony:
    """Another colony exactly like ``home``'s world, on an unclaimed rock.

    Used to prove that output is linear in colony count: whatever the second one
    produces, the fourth must produce the same.
    """
    world = next(
        w
        for w in session.scalars(select(World).order_by(World.id))
        if w.colony is None
    )
    world.habitability = 0.9
    give_deposits(world, iron=0.02)
    colony = Colony(
        world_id=world.id,
        civ_id=civ.id,
        name=f"Twin {world.id}",
        population=1_000_000.0,
        infrastructure=1.0,
        founded_tick=0,
        stockpile={},
        labor=balanced_allocation(),
    )
    colony.management_mode = "manual"
    session.add(colony)
    session.flush()
    return colony
