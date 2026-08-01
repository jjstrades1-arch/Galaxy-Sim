"""Moving goods between colonies.

Stockpiles are local, so these tests cover the only thing that connects them.
The one that matters most is
:func:`test_a_supply_route_keeps_a_dead_world_alive` -- that is the payoff the
whole design is arranged around: the richest worlds cannot feed themselves, and
a standing route is what makes holding one possible.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.colony.labor import balanced_allocation
from galaxysim.materials import IRON, WATER
from galaxysim.core.space import distance
from galaxysim.engine import intents
from galaxysim.engine.resolvers.logistics import BASE_THROUGHPUT_PER_HOUR, throughput_per_hour
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Building, Colony, Event, Fleet, IntentStatus, World
from tests.conftest import civ_by_name, give_deposits, home_colony, new_universe


def _sibling_outpost(session, civ, home, *, habitability=0.0, stockpile=None):
    """A colony on the nearest unclaimed world, so test routes stay short.

    Prefers the home system, but not every seed leaves a spare world there, so
    it falls back to the closest system that has one.
    """
    origin = home.world.system.position
    unclaimed = [w for w in session.scalars(select(World)).all() if w.colony is None]
    assert unclaimed, "the test universe has no unclaimed world left"
    world = min(unclaimed, key=lambda w: (distance(origin, w.system.position), w.id))
    world.habitability = habitability
    world.world_type = "barren"
    give_deposits(world, iron=0.02)
    world.slots = 5
    colony = Colony(
        world_id=world.id,
        civ_id=civ.id,
        name="Deep Rock",
        population=3.0,
        infrastructure=1.0,
        founded_tick=0,
        stockpile=dict(stockpile or {}),
        labor=balanced_allocation(),
    )
    session.add(colony)
    session.flush()
    return colony


def _freighter(session, civ, home, capacity=200.0):
    fleet = Fleet(
        universe_id=home.civ.universe_id,
        civ_id=civ.id,
        name="Hauler",
        strength=0.5,
        colony_pods=0,
        speed_ly_per_hour=1.0,
        cargo={},
        cargo_capacity=capacity,
        x=home.world.system.x,
        y=home.world.system.y,
        z=home.world.system.z,
    )
    session.add(fleet)
    session.flush()
    return fleet


# ------------------------------------------------------------ one-off moves


def test_cargo_moves_from_one_colony_to_another():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=901, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = {WATER: 300.0}
        # Silence production at both ends so this measures the transfer itself.
        # With geology-derived yields a rich world can out-produce the cargo
        # being moved, which would drown the thing under test.
        give_deposits(home.world)
        outpost = _sibling_outpost(session, civ, home)
        give_deposits(outpost.world)
        fleet = _freighter(session, civ, home)
        intents.transfer_cargo(session, civ, fleet.id, home.id, {WATER: 100.0}, loading=True)
        fleet_id, outpost_id, home_id = fleet.id, outpost.id, home.id

    # Loading is throughput-limited, so it takes a while at a colony with only
    # the base rate.
    run_ticks(engine, universe_id, 30)
    with open_session(engine) as session:
        fleet = session.get(Fleet, fleet_id)
        assert fleet.cargo.get(WATER, 0.0) == pytest.approx(100.0)
        # No duplication: the hundred aboard came off the ground. Not exactly
        # 200 left, because the colony also breathes some of its own water
        # while the hold fills -- so the invariant is that at least the cargo
        # left, never that nothing else moved.
        remaining = session.get(Colony, home_id).stockpile[WATER]
        assert remaining <= 200.0
        assert remaining > 150.0, "only life support should have taken the rest"

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        intents.transfer_cargo(
            session, civ, fleet_id, outpost_id, {WATER: 100.0}, loading=False
        )

    run_ticks(engine, universe_id, 30)
    with open_session(engine) as session:
        # Slightly under 100: the outpost is uninhabitable and starts breathing
        # the delivery the moment it lands.
        assert session.get(Colony, outpost_id).stockpile[WATER] == pytest.approx(100.0, rel=0.05)
        assert session.get(Fleet, fleet_id).cargo_tonnage == pytest.approx(0.0)


def test_a_hold_cannot_be_overfilled():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=902, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = {WATER: 5000.0}
        fleet = _freighter(session, civ, home, capacity=50.0)
        intents.transfer_cargo(session, civ, fleet.id, home.id, {WATER: 1000.0})
        fleet_id = fleet.id

    run_ticks(engine, universe_id, 60)

    with open_session(engine) as session:
        fleet = session.get(Fleet, fleet_id)
        assert fleet.cargo_tonnage == pytest.approx(50.0)
        assert fleet.cargo_space == pytest.approx(0.0)


def test_a_transfer_waits_for_the_fleet_to_arrive():
    """Queue "fly there, then deliver" in one sitting, like colonization."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=903, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        fleet = _freighter(session, civ, home)
        fleet.x += 20.0  # parked well away
        intents.transfer_cargo(session, civ, fleet.id, home.id, {WATER: 10.0})

    run_ticks(engine, universe_id, 2)

    with open_session(engine) as session:
        intent = session.scalar(select(intents.Intent).where(intents.Intent.kind == "transfer_cargo"))
        assert intent.status == IntentStatus.QUEUED.value
        assert intent.result == "awaiting fleet arrival"


def test_spaceports_raise_throughput():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=904, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        # Bootstrap gives the capital a spaceport.
        assert throughput_per_hour(home) > BASE_THROUGHPUT_PER_HOUR

        bare = _sibling_outpost(session, civ, home)
        assert throughput_per_hour(bare) == pytest.approx(BASE_THROUGHPUT_PER_HOUR)

        session.add(
            Building(colony_id=bare.id, kind="spaceport", work_remaining=0.0, completed_tick=0)
        )
        session.flush()
        session.refresh(bare)
        assert throughput_per_hour(bare) > BASE_THROUGHPUT_PER_HOUR


# --------------------------------------------------------- standing routes


def test_a_supply_route_keeps_a_dead_world_alive():
    """The payoff the whole design is arranged around.

    A barren world yields metal but no water, so it suffocates on its own.
    One standing route, set up once, keeps it alive indefinitely -- and the
    player never has to log in again to sustain it.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=905, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = {WATER: 100000.0, IRON: 1000.0}
        home.world.habitability = 1.0  # the capital needs no life support itself
        outpost = _sibling_outpost(session, civ, home, stockpile={WATER: 30.0})
        fleet = _freighter(session, civ, home, capacity=300.0)

        intents.supply_route(
            session, civ, fleet.id, home.id, outpost.id, {WATER: 250.0}
        )
        outpost_id = outpost.id
        founding_population = outpost.population

    # Two simulated weeks. Unsupplied, 30 water is about three days of air.
    run_ticks(engine, universe_id, 336)

    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        assert outpost.population == pytest.approx(founding_population, rel=0.05), (
            "the route should have kept it alive"
        )
        assert outpost.stockpile.get(IRON, 0.0) > 0, "and it should have been mining throughout"
        assert session.scalars(
            select(Event).where(Event.kind == "supply_delivered")
        ).all(), "deliveries should be logged"


def test_cutting_the_route_kills_the_colony():
    """The other half: the route is load-bearing, not decorative."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=906, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = {WATER: 100000.0}
        home.world.habitability = 1.0
        outpost = _sibling_outpost(session, civ, home, stockpile={WATER: 30.0})
        fleet = _freighter(session, civ, home, capacity=60.0)
        # A small manifest on purpose: a big delivery would leave months of air
        # banked and the colony would outlive the test rather than the cut.
        route = intents.supply_route(
            session, civ, fleet.id, home.id, outpost.id, {WATER: 40.0}
        )
        outpost_id, route_id = outpost.id, route.id

    run_ticks(engine, universe_id, 168)
    with open_session(engine) as session:
        supplied = session.get(Colony, outpost_id).population
        assert supplied > 0

    # Cut it, and spend down the buffer the deliveries banked.
    #
    # A standing route ships its full manifest every trip whether or not the
    # destination needs it, so a well-supplied outpost accumulates months of
    # air. Simulating that drain would take thousands of ticks and would be
    # testing the size of the buffer, not the mechanic. What is under test here
    # is that nothing else keeps the colony alive once the route stops.
    with open_session(engine) as session:
        route = session.get(intents.Intent, route_id)
        intents.cancel(session, route)
        outpost = session.get(Colony, outpost_id)
        outpost.stockpile[WATER] = 5.0  # about a day of air

    run_ticks(engine, universe_id, 500)

    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        assert outpost.population < supplied * 0.5, "without resupply it should be dying"
        assert session.scalars(
            select(Event).where(Event.kind == "life_support_failing")
        ).all()


def test_a_route_runs_without_further_orders():
    """Standing means standing: many round trips from one order."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=907, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = {WATER: 100000.0}
        home.world.habitability = 1.0
        outpost = _sibling_outpost(session, civ, home, stockpile={WATER: 50.0})
        fleet = _freighter(session, civ, home, capacity=100.0)
        intents.supply_route(session, civ, fleet.id, home.id, outpost.id, {WATER: 80.0})
        route_id = session.scalar(
            select(intents.Intent).where(intents.Intent.kind == "supply_route")
        ).id

    run_ticks(engine, universe_id, 400)

    with open_session(engine) as session:
        route = session.get(intents.Intent, route_id)
        assert route.status == IntentStatus.IN_PROGRESS.value, "a route never completes"
        deliveries = session.scalars(
            select(Event).where(Event.kind == "supply_delivered")
        ).all()
        assert len(deliveries) >= 3, f"expected repeated round trips, saw {len(deliveries)}"


def test_a_route_between_other_peoples_colonies_is_rejected():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=908, civs=("Terrans", "Vex"), seconds_per_tick=3600)

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        home = home_colony(session, terrans)
        their_colony = home_colony(session, vex)
        fleet = _freighter(session, terrans, home)
        intents.supply_route(
            session, terrans, fleet.id, home.id, their_colony.id, {WATER: 10.0}
        )

    run_ticks(engine, universe_id, 3)

    with open_session(engine) as session:
        route = session.scalar(
            select(intents.Intent).where(intents.Intent.kind == "supply_route")
        )
        assert route.status == IntentStatus.FAILED.value
        assert "your own colonies" in route.result


def test_a_route_needs_two_distinct_colonies():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=909, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        fleet = _freighter(session, civ, home)
        with pytest.raises(ValueError):
            intents.supply_route(session, civ, fleet.id, home.id, home.id, {WATER: 1.0})
