"""The core loop: queue intents, tick, see them resolve.

Build-order step 1 is "prove queue -> tick resolves -> state persists". These
tests are that proof, plus the offline-progression guarantee the async design
depends on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.colony.labor import LIFE_SUPPORT, RESEARCH
from galaxysim.materials import IRON, STEEL
from galaxysim.core.space import Vec3, distance
from galaxysim.engine import intents
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import (
    Civ,
    Colony,
    Event,
    Fleet,
    Intent,
    IntentStatus,
    StarSystem,
    Universe,
)
from tests.conftest import civ_by_name, held, new_universe, take_manual_control


@pytest.fixture
def game():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=2024, civs=("Terrans", "Vex"))
    return engine, universe_id


@pytest.fixture
def hourly_game():
    """The same opening on an hourly clock.

    For tests that measure days rather than minutes. Cadence cannot change what
    they assert -- ``test_growth_is_equivalent_across_cadences`` is the proof --
    so running them at five-minute resolution is twelve times the work for
    identical results.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=2024, civs=("Terrans", "Vex"), seconds_per_tick=3600
    )
    return engine, universe_id


def test_tick_advances_the_clock(game):
    engine, universe_id = game
    results = run_ticks(engine, universe_id, 3)

    assert [r.tick for r in results] == [1, 2, 3]
    with open_session(engine) as session:
        assert session.get(Universe, universe_id).tick_number == 3


def test_offline_civ_still_produces(game):
    """Nobody queued anything. Production must run anyway.

    An async game where sleeping costs you the economy is not an async game.
    """
    engine, universe_id = game

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        # No governor spending the stockpile: this test is about production
        # continuing for an absent player, not about what a governor buys.
        take_manual_control(session, civ)
        before = held(session, civ, IRON)

    run_ticks(engine, universe_id, 24)

    with open_session(engine) as session:
        after = held(session, civ_by_name(session, universe_id, "Terrans"), IRON)

    assert after > before


def test_move_order_takes_wall_clock_time_and_arrives(game):
    engine, universe_id = game

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == terrans.id).order_by(Fleet.id))
        target = session.scalars(
            select(StarSystem)
            .where(StarSystem.universe_id == universe_id)
            .order_by(StarSystem.id.desc())
        ).first()
        fleet_id, start = fleet.id, fleet.position
        destination = Vec3(target.x, target.y, target.z)
        intents.move_fleet_to_system(session, terrans, fleet_id, target)

    journey_ly = distance(start, destination)
    assert journey_ly > 1.0, "test needs a non-trivial journey"

    run_ticks(engine, universe_id, 1)
    with open_session(engine) as session:
        fleet = session.get(Fleet, fleet_id)
        assert fleet.in_transit
        assert fleet.arrival_tick > 1

    # At 1 ly/hour and 5-minute ticks, the trip takes 12 ticks per light-year.
    run_ticks(engine, universe_id, int(journey_ly * 12) + 2)

    with open_session(engine) as session:
        fleet = session.get(Fleet, fleet_id)
        assert not fleet.in_transit
        assert distance(fleet.position, destination) < 0.01


def test_colonize_waits_for_the_fleet_then_settles(game):
    """Move and colonize queued together in one sitting -- the async pattern."""
    engine, universe_id = game

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == terrans.id).order_by(Fleet.id))
        target = next(
            system
            for system in session.scalars(
                select(StarSystem)
                .where(StarSystem.universe_id == universe_id)
                .order_by(StarSystem.id)
            )
            if any(w.habitability > 0 and w.colony is None for w in system.worlds)
            and distance(Vec3(system.x, system.y, system.z), fleet.position) > 0.01
        )
        world = next(
            w for w in sorted(target.worlds, key=lambda w: w.id)
            if w.habitability > 0 and w.colony is None
        )
        world_id, fleet_id = world.id, fleet.id
        journey_ly = distance(Vec3(target.x, target.y, target.z), fleet.position)
        intents.move_fleet_to_system(session, terrans, fleet_id, target)
        intents.colonize(session, terrans, fleet_id, world_id)
        colonies_before = len(
            session.scalars(select(Colony).where(Colony.civ_id == terrans.id)).all()
        )

    run_ticks(engine, universe_id, 2)
    with open_session(engine) as session:
        colonize_intent = session.scalar(
            select(Intent).where(Intent.kind == "colonize").order_by(Intent.id)
        )
        assert colonize_intent.status == IntentStatus.QUEUED.value
        assert colonize_intent.result == "expedition loaded, awaiting fleet arrival"

    # 12 ticks per light-year at 1 ly/hour and 5-minute ticks, plus the six
    # hours of settling once the fleet is down.
    run_ticks(engine, universe_id, int(journey_ly * 12) + 100)

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        colonies = session.scalars(select(Colony).where(Colony.civ_id == terrans.id)).all()
        assert len(colonies) == colonies_before + 1
        assert session.get(Fleet, fleet_id).colony_pods == 0


def test_an_expedition_is_paid_for_where_it_departs(game):
    """The bug that quietly ended every AI civilization's expansion.

    An expedition used to be outfitted on *arrival*, from whichever colony was
    nearest the destination -- which on a frontier run is the outpost you
    founded last week, and which has nothing. The order then never failed and
    never completed: it reported an empty warehouse at the edge of your
    territory forever, and since a civ only settles one world at a time, that
    single stuck order stopped it expanding again for the rest of the game.

    A colony ship is loaded before it leaves. That is both the physical reading
    and the one that cannot deadlock, because the place it is charged is a
    place that has something.
    """
    engine, universe_id = game

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        home = session.scalar(select(Colony).where(Colony.civ_id == terrans.id).order_by(Colony.id))
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == terrans.id).order_by(Fleet.id))
        target = next(
            system
            for system in session.scalars(
                select(StarSystem)
                .where(StarSystem.universe_id == universe_id)
                .order_by(StarSystem.id)
            )
            if any(w.colony is None for w in system.worlds)
            and distance(Vec3(system.x, system.y, system.z), fleet.position) > 0.01
        )
        world = next(w for w in sorted(target.worlds, key=lambda w: w.id) if w.colony is None)
        before = dict(home.stockpile)
        intents.move_fleet_to_system(session, terrans, fleet.id, target)
        intents.colonize(session, terrans, fleet.id, world.id)
        home_id = home.id

    run_ticks(engine, universe_id, 3)

    with open_session(engine) as session:
        home = session.get(Colony, home_id)
        order = session.scalar(select(Intent).where(Intent.kind == "colonize"))
        assert order.payload["outfitted_colony_id"] == home_id, (
            "the capital should have loaded the ship before it left"
        )
        # Something was actually taken out of the warehouse it departed from.
        spent = {k: before.get(k, 0.0) - home.stockpile.get(k, 0.0) for k in before}
        assert any(amount > 0 for amount in spent.values())


def test_research_is_a_standing_order(hourly_game):
    """A research programme keeps buying steps without further input."""
    engine, universe_id = hourly_game

    with open_session(engine) as session:
        intents.research(session, civ_by_name(session, universe_id, "Terrans"))

    # Two days of simulated time. Deliberately slow: a starting civ is expected
    # to be a step or two in after two days, not a dozen.
    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        assert terrans.techs_known >= 1
        assert terrans.research_invested > 0
        # Vex never ordered research, so it banks points but spends none.
        assert vex.techs_known == 0
        assert vex.research_progress > 0


def test_fleets_only_fight_when_hostility_is_declared(game):
    """Meeting a stranger is not a war.

    Diplomacy is player-driven, so the attack order is the entire mechanical
    surface of hostility. Without it, co-located fleets must pass peacefully.
    """
    engine, universe_id = game

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        terran_fleet = session.scalar(
            select(Fleet).where(Fleet.civ_id == terrans.id).order_by(Fleet.id)
        )
        vex_fleet = session.scalar(select(Fleet).where(Fleet.civ_id == vex.id).order_by(Fleet.id))
        # Put them nose to nose.
        vex_fleet.x, vex_fleet.y, vex_fleet.z = terran_fleet.x, terran_fleet.y, terran_fleet.z
        terran_id, vex_id, vex_civ_id = terran_fleet.id, vex_fleet.id, vex.id
        strength_before = terran_fleet.strength

    run_ticks(engine, universe_id, 5)
    with open_session(engine) as session:
        assert session.get(Fleet, terran_id).strength == strength_before

    with open_session(engine) as session:
        intents.attack(session, civ_by_name(session, universe_id, "Vex"),
                       civ_by_name(session, universe_id, "Terrans").id)

    run_ticks(engine, universe_id, 5)
    with open_session(engine) as session:
        # Both sides take losses: combat is mutual even when only one declared.
        assert session.get(Fleet, terran_id).strength < strength_before
        assert session.get(Fleet, vex_id).strength < strength_before
        assert vex_civ_id is not None


def test_combat_destroys_fleets_and_logs_it(game):
    engine, universe_id = game

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        terran_fleet = session.scalar(
            select(Fleet).where(Fleet.civ_id == terrans.id).order_by(Fleet.id)
        )
        vex_fleet = session.scalar(select(Fleet).where(Fleet.civ_id == vex.id).order_by(Fleet.id))
        vex_fleet.x, vex_fleet.y, vex_fleet.z = terran_fleet.x, terran_fleet.y, terran_fleet.z
        # Wildly lopsided, so the outcome is not in doubt.
        vex_fleet.strength = 100.0
        terran_id = terran_fleet.id
        intents.attack(session, vex, terrans.id)

    run_ticks(engine, universe_id, 60)

    with open_session(engine) as session:
        assert session.get(Fleet, terran_id) is None
        destroyed = session.scalars(
            select(Event).where(Event.kind == "fleet_destroyed")
        ).all()
        assert destroyed


def test_build_order_charges_up_front_and_delivers(game):
    engine, universe_id = game

    with open_session(engine) as session:
        vex = civ_by_name(session, universe_id, "Vex")
        take_manual_control(session, vex)
        colony = session.scalar(select(Colony).where(Colony.civ_id == vex.id).order_by(Colony.id))
        vex_id = vex.id
        fleets_before = len(session.scalars(select(Fleet).where(Fleet.civ_id == vex.id)).all())
        intents.build_fleet(session, vex, colony.id, 2.0, name="Vex Second Fleet")
        colony_id = colony.id

    run_ticks(engine, universe_id, 1)
    with open_session(engine) as session:
        # A developed capital lays down a two-strength hull inside one tick, so
        # what "charged up front" means here is that the yard paid for it --
        # checked below against a yard that cannot.
        assert session.scalar(
            select(Intent).where(Intent.kind == "build_fleet", Intent.civ_id == vex_id)
        ).status in (IntentStatus.IN_PROGRESS.value, IntentStatus.COMPLETED.value)

    # Long enough to finish: construction only gets the share of industry that
    # refining leaves it, so a hull takes about twice as many hours as it did
    # when ore went straight into ships.
    run_ticks(engine, universe_id, 300)
    with open_session(engine) as session:
        vex = civ_by_name(session, universe_id, "Vex")
        fleets = session.scalars(select(Fleet).where(Fleet.civ_id == vex.id)).all()
        assert len(fleets) == fleets_before + 1
        assert any(f.name == "Vex Second Fleet" for f in fleets)

    # And the other half: a yard with nothing coming in never starts one.
    #
    # It *gathers* rather than refusing outright -- a yard takes delivery of
    # what it can each hour and holds it against the order, because an
    # all-or-nothing bill is a bill nobody can save for. A capital's governor
    # spends steel the hour it is refined, so a large purchase would be
    # unbuyable at any income if it had to be met in one instant. What still
    # must not happen is work beginning on an unpaid hull.
    with open_session(engine) as session:
        vex = civ_by_name(session, universe_id, "Vex")
        colony = session.get(Colony, colony_id)
        colony.stockpile = {}
        intents.set_labor(session, colony, {RESEARCH: 1.0})  # and cannot mine more
        intents.build_fleet(session, vex, colony_id, 2.0, name="Vex Third Fleet")

    run_ticks(engine, universe_id, 2)
    with open_session(engine) as session:
        order = session.scalar(
            select(Intent).where(Intent.kind == "build_fleet", Intent.payload.contains("Third"))
        )
        assert order.status == IntentStatus.QUEUED.value
        assert "gathering materials" in order.result
        assert "short" in order.result


def test_a_yard_can_save_up_for_something_it_cannot_buy_outright(game):
    """A big purchase must be slow, not impossible.

    This is the difference between a price and a wall, and the game had a wall.
    A colony pod costs more steel than a capital ever holds at one instant --
    its governor spends steel on industry the hour it is refined -- so an
    all-or-nothing bill meant eight AI civilizations stopped dead at thirty-odd
    colonies for sixty simulated days, each with somewhere to settle, a ship
    ready to settle it, and zero tonnes in the bank. They could afford it over a
    week and could not afford it in an hour, and only the second was being
    asked.
    """
    engine, universe_id = game

    from galaxysim.materials import ALLOYS, ELECTRONICS, STEEL

    with open_session(engine) as session:
        vex = civ_by_name(session, universe_id, "Vex")
        take_manual_control(session, vex)
        colony = session.scalar(select(Colony).where(Colony.civ_id == vex.id).order_by(Colony.id))
        # Everyone onto life support, so the yard mines nothing, refines
        # nothing and researches nothing: the only materials it ever sees are
        # the ones delivered below. (Not research -- that consumes electronics,
        # and the laboratories would quietly outbid the shipyard for them.)
        intents.set_labor(session, colony, {LIFE_SUPPORT: 1.0})
        colony.stockpile = {}
        intents.build_fleet(session, vex, colony.id, 2.0, name="Vex Patient Fleet")
        colony_id = colony.id

    # One tick with nothing: it must be waiting, not building.
    run_ticks(engine, universe_id, 1)
    with open_session(engine) as session:
        order = session.scalar(
            select(Intent).where(Intent.kind == "build_fleet", Intent.payload.contains("Patient"))
        )
        assert order.status == IntentStatus.QUEUED.value
        assert "gathering" in order.result

    # Now drip-feed it: never the whole bill at once, plenty of it over time.
    # A strength-2 hull wants 210,000 steel, 126,000 alloys, 64,000 electronics.
    for _ in range(12):
        with open_session(engine) as session:
            colony = session.get(Colony, colony_id)
            for resource, amount in (
                (STEEL, 20_000.0),
                (ALLOYS, 12_000.0),
                (ELECTRONICS, 6_000.0),
            ):
                colony.stockpile[resource] = colony.stockpile.get(resource, 0.0) + amount
        run_ticks(engine, universe_id, 1)

    with open_session(engine) as session:
        order = session.scalar(
            select(Intent).where(Intent.kind == "build_fleet", Intent.payload.contains("Patient"))
        )
        assert order.status != IntentStatus.QUEUED.value, (
            "twelve deliveries cover the bill; the yard should have laid it "
            f"down by now, but it says {order.result!r}"
        )
        # And the deliveries went into the hull rather than sitting in the
        # warehouse: twelve loads of 20,000 is 240,000 tonnes of steel against a
        # bill of 210,000, so what is left should be the surplus and nothing
        # more. The yard took each delivery as it arrived.
        colony = session.get(Colony, colony_id)
        assert colony.stockpile.get(STEEL, 0.0) == pytest.approx(30_000.0, abs=1_000.0)


def test_impossible_orders_fail_with_a_reason(game):
    """A failed order must say why -- it is the player's only feedback."""
    engine, universe_id = game

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        intents.move_fleet(session, terrans, 9999, 1.0, 1.0, 1.0)

    run_ticks(engine, universe_id, 1)

    with open_session(engine) as session:
        intent = session.scalar(select(Intent).where(Intent.kind == "move_fleet"))
        assert intent.status == IntentStatus.FAILED.value
        assert intent.result == "no such fleet"
        assert session.scalars(select(Event).where(Event.kind == "intent_failed")).all()


def test_events_are_scoped_to_a_civ(game):
    """Each civ reads its own 'what happened while you were away' log."""
    engine, universe_id = game

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        colony = session.scalar(
            select(Colony).where(Colony.civ_id == terrans.id).order_by(Colony.id)
        )
        intents.build_fleet(session, terrans, colony.id, 1.0)
        terran_id = terrans.id

    run_ticks(engine, universe_id, 5)

    with open_session(engine) as session:
        mine = session.scalars(select(Event).where(Event.civ_id == terran_id)).all()
        assert mine
        assert all(e.civ_id == terran_id for e in mine)


def test_cancelled_order_does_not_resolve(game):
    engine, universe_id = game

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        colony = session.scalar(
            select(Colony).where(Colony.civ_id == terrans.id).order_by(Colony.id)
        )
        intent = intents.build_fleet(session, terrans, colony.id, 1.0)
        intents.cancel(session, intent)
        fleets_before = len(session.scalars(select(Fleet).where(Fleet.civ_id == terrans.id)).all())
        terran_id = terrans.id

    run_ticks(engine, universe_id, 100)

    with open_session(engine) as session:
        fleets = session.scalars(select(Fleet).where(Fleet.civ_id == terran_id)).all()
        assert len(fleets) == fleets_before


def test_each_civ_starts_on_its_own_habitable_world():
    """Fair starts are structural, not a reroll."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, civs=("A", "B", "C", "D"), system_count=12)

    with open_session(engine) as session:
        colonies = session.scalars(select(Colony).order_by(Colony.id)).all()
        assert len(colonies) == 4
        assert all(c.world.habitability > 0 for c in colonies)
        systems = [c.world.system_id for c in colonies]
        assert len(set(systems)) == 4, "civs must not share a starting system"

        civs = session.scalars(select(Civ).order_by(Civ.id)).all()
        # Starting goods sit on the homeworld, not in a treasury.
        assert all(held(session, c, STEEL) > 0 for c in civs)
        assert all(c.stockpile.get(STEEL, 0.0) > 0 for c in colonies)
