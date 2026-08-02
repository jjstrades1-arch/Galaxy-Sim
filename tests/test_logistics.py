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
from galaxysim.materials.catalogue import ALLOYS, CARBON, FUEL, STEEL
from galaxysim.core.space import distance
from galaxysim.engine import intents
from galaxysim.engine.resolvers.logistics import (
    BACKHAUL_RESERVE,
    BASE_THROUGHPUT_PER_HOUR,
    throughput_per_hour,
)
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Building, Colony, Event, Fleet, IntentStatus, World
from tests.conftest import (
    OUTPOST_POPULATION,
    civ_by_name,
    feed,
    give_deposits,
    home_colony,
    new_universe,
    take_manual_control,
)


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
    colony = Colony(
        world_id=world.id,
        civ_id=civ.id,
        name="Deep Rock",
        population=OUTPOST_POPULATION,
        infrastructure=1.0,
        founded_tick=0,
        stockpile=dict(stockpile or {}),
        labor=balanced_allocation(),
    )
    session.add(colony)
    session.flush()
    # These tests are about shipping water. Keep the larder stocked so a colony
    # never dies of hunger while we are measuring whether it died of thirst.
    feed(colony)
    return colony


def _freighter(session, civ, home, capacity=200_000.0):
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
        home.stockpile = {WATER: 300_000.0}
        # Silence production at both ends so this measures the transfer itself.
        # With geology-derived yields a rich world can out-produce the cargo
        # being moved, which would drown the thing under test.
        give_deposits(home.world)
        outpost = _sibling_outpost(session, civ, home)
        give_deposits(outpost.world)
        fleet = _freighter(session, civ, home)
        intents.transfer_cargo(session, civ, fleet.id, home.id, {WATER: 5_000.0}, loading=True)
        fleet_id, outpost_id, home_id = fleet.id, outpost.id, home.id

    # Loading is throughput-limited, so it takes a while at a colony with only
    # the base rate.
    run_ticks(engine, universe_id, 30)
    with open_session(engine) as session:
        fleet = session.get(Fleet, fleet_id)
        assert fleet.cargo.get(WATER, 0.0) == pytest.approx(5_000.0)
        # No duplication: the hundred aboard came off the ground. Not exactly
        # 200 left, because the colony also breathes some of its own water
        # while the hold fills -- so the invariant is that at least the cargo
        # left, never that nothing else moved.
        remaining = session.get(Colony, home_id).stockpile[WATER]
        assert remaining <= 295_000.0
        assert remaining > 250_000.0, "only life support should have taken the rest"

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        intents.transfer_cargo(
            session, civ, fleet_id, outpost_id, {WATER: 5_000.0}, loading=False
        )

    run_ticks(engine, universe_id, 30)
    with open_session(engine) as session:
        # Under the full manifest: the outpost is uninhabitable and starts
        # breathing the delivery the moment it lands. It has no spaceport
        # either, so it takes the cargo at the bare rate -- which is exactly why
        # the next test cares about ports.
        landed = session.get(Colony, outpost_id).stockpile[WATER]
        assert 3_000.0 < landed <= 5_000.0
        assert session.get(Fleet, fleet_id).cargo_tonnage == pytest.approx(0.0)


def test_a_hold_cannot_be_overfilled():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=902, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = {WATER: 5_000_000.0}
        fleet = _freighter(session, civ, home, capacity=50_000.0)
        intents.transfer_cargo(session, civ, fleet.id, home.id, {WATER: 1_000_000.0})
        fleet_id = fleet.id

    run_ticks(engine, universe_id, 60)

    with open_session(engine) as session:
        fleet = session.get(Fleet, fleet_id)
        assert fleet.cargo_tonnage == pytest.approx(50_000.0)
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
        intents.transfer_cargo(session, civ, fleet.id, home.id, {WATER: 10_000.0})

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
        home.stockpile = {WATER: 100_000_000.0, IRON: 1_000_000.0}
        home.world.habitability = 1.0  # the capital needs no life support itself
        outpost = _sibling_outpost(session, civ, home, stockpile={WATER: 6_000.0})
        fleet = _freighter(session, civ, home, capacity=300_000.0)

        intents.supply_route(
            session, civ, fleet.id, home.id, outpost.id, {WATER: 20_000.0}
        )
        outpost_id = outpost.id
        founding_population = outpost.population

    # Two simulated weeks. Unsupplied, 30 water is about three days of air.
    run_ticks(engine, universe_id, 336)

    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        assert outpost.population >= founding_population, (
            "the route should have kept it alive -- and a supplied outpost grows "
            "toward what its habitats hold"
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
        home.stockpile = {WATER: 100_000_000.0}
        home.world.habitability = 1.0
        outpost = _sibling_outpost(session, civ, home, stockpile={WATER: 6_000.0})
        fleet = _freighter(session, civ, home, capacity=60_000.0)
        # A small manifest on purpose: a big delivery would leave months of air
        # banked and the colony would outlive the test rather than the cut.
        route = intents.supply_route(
            session, civ, fleet.id, home.id, outpost.id, {WATER: 40_000.0}
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
        outpost.stockpile[WATER] = 1_200.0  # about a day of air

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
        home.stockpile = {WATER: 100_000_000.0}
        home.world.habitability = 1.0
        outpost = _sibling_outpost(session, civ, home, stockpile={WATER: 10_000.0})
        fleet = _freighter(session, civ, home, capacity=100_000.0)
        intents.supply_route(session, civ, fleet.id, home.id, outpost.id, {WATER: 8_000.0})
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


# -------------------------------------------------------------- the backhaul


def test_a_freighter_comes_home_loaded():
    """The return leg is half the journey, and it used to be spent empty."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=910, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        take_manual_control(session, civ)
        home = home_colony(session, civ)
        home.world.habitability = 1.0
        give_deposits(home.world)  # the capital digs nothing up itself
        home.stockpile = {WATER: 100_000_000.0, FUEL: 1.0e8, ALLOYS: 1.0e8}
        feed(home)

        outpost = _sibling_outpost(
            session, civ, home, stockpile={WATER: 20_000.0, IRON: 400_000.0}
        )
        fleet = _freighter(session, civ, home, capacity=150_000.0)
        intents.supply_route(session, civ, fleet.id, home.id, outpost.id, {WATER: 10_000.0})
        home_id = home.id

    run_ticks(engine, universe_id, 336)

    with open_session(engine) as session:
        home = session.get(Colony, home_id)

        assert session.scalars(
            select(Event).where(Event.kind == "backhaul_landed")
        ).all(), "the return leg should be landing cargo at the capital"

        # The capital mines no iron and cannot smelt any -- there is no carbon
        # anywhere in this universe -- so every tonne it holds was carried there
        # by a freighter that used to make the same journey empty.
        assert home.stockpile.get(IRON, 0.0) > 20_000.0

        # The outpost went on being supplied throughout. Ore riding home must not
        # cost the route the job it exists to do: one port serves both halves of
        # the trip, and a hold filled with ore is a colony left thirsty.
        deliveries = session.scalars(
            select(Event).where(Event.kind == "supply_delivered")
        ).all()
        assert len(deliveries) >= 3, f"expected repeated deliveries, saw {len(deliveries)}"


def test_the_backhaul_leaves_a_working_reserve():
    """Surplus goes; the stock the colony needs to keep working stays.

    Checked directly rather than through a fortnight of simulation, because what
    is under test is the rule, and a colony that goes on mining while the hold
    fills makes the rule hard to see from the outside.
    """
    from galaxysim.engine.resolvers.logistics import _backhaul_manifest

    class _Hold:
        cargo_space = 1_000_000.0

    class _Colony:
        stockpile = {
            IRON: BACKHAUL_RESERVE + 30_000.0,
            CARBON: BACKHAUL_RESERVE - 5_000.0,  # below the reserve: stays put
            STEEL: 500_000.0,  # refined, and the destination just made it
            WATER: 900_000.0,  # consumable, and it was probably just delivered
        }

    manifest = _backhaul_manifest(_Hold(), _Colony(), 1_000_000.0)
    assert manifest == {IRON: 30_000.0}


def test_the_backhaul_shares_the_hold_between_ores():
    """A smelter needs iron *and* carbon, so one ore must not take the ship."""
    from galaxysim.engine.resolvers.logistics import _backhaul_manifest

    class _Hold:
        cargo_space = 1_000_000.0

    class _Colony:
        stockpile = {
            CARBON: BACKHAUL_RESERVE + 100_000.0,
            IRON: BACKHAUL_RESERVE + 300_000.0,
        }

    manifest = _backhaul_manifest(_Hold(), _Colony(), 40_000.0)
    assert sum(manifest.values()) == pytest.approx(40_000.0)
    # In proportion to what is spare, so the hold mirrors the world's geology
    # rather than the alphabet.
    assert manifest[IRON] == pytest.approx(30_000.0)
    assert manifest[CARBON] == pytest.approx(10_000.0)


def test_ore_reaches_the_industry_that_can_use_it():
    """The point of the backhaul: mined in one place, refined in another.

    A barren world has the geology and no industry; the capital has the industry
    and no geology. Neither can make steel alone, and until the return leg
    carried anything neither ever did.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=911, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        take_manual_control(session, civ)
        home = home_colony(session, civ)
        home.world.habitability = 1.0
        give_deposits(home.world)
        # Strip the opening warehouses: whatever steel exists at the end has to
        # have been made here out of ore that was mined somewhere else.
        home.stockpile = {WATER: 100_000_000.0}
        feed(home)

        outpost = _sibling_outpost(
            session,
            civ,
            home,
            stockpile={WATER: 20_000.0, IRON: 300_000.0, CARBON: 80_000.0},
        )
        give_deposits(outpost.world, iron=0.02, carbon=0.004)
        fleet = _freighter(session, civ, home, capacity=150_000.0)
        intents.supply_route(session, civ, fleet.id, home.id, outpost.id, {WATER: 10_000.0})
        home_id = home.id

    run_ticks(engine, universe_id, 336)

    with open_session(engine) as session:
        home = session.get(Colony, home_id)
        assert home.stockpile.get(STEEL, 0.0) > 0.0, (
            "the capital should be smelting ore it never mined"
        )


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
            session, terrans, fleet.id, home.id, their_colony.id, {WATER: 10_000.0}
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
            intents.supply_route(session, civ, fleet.id, home.id, home.id, {WATER: 1_000.0})
