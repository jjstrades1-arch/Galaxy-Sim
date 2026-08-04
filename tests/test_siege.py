"""Blockade, siege and capture -- the part of a war that touches the map.

Before this, two civilizations could destroy each other's navies for a month and
finish holding exactly the worlds they started with. These tests are about the
three things that changed, and each one is really a test that a *consequence*
arrives rather than that a number moves:

* a blockade stops cargo, and the outpost at the far end of the route dies;
* a siege wears down what is actually there, so a frontier world falls in hours
  and a homeworld cannot be taken from orbit at all;
* taking a world costs a colony pod, so annexation is an investment and
  starving a rival without absorbing them is a strategy that exists.

A note on how these are staged, because it looks like a mistake and is not: the
blockading fleet is parked a fraction of a light year *off* the star rather than
on it. Combat matches fleets at the same exact position; a blockade is a cordon
around a system, and holds out to :data:`siege.BLOCKADE_RANGE_LY`. Putting the
besieger in that gap isolates the mechanic under test from the shooting -- and
it is the same gap that stops a freighter slipping past a battle fleet by
rounding error.
"""

from __future__ import annotations

from sqlalchemy import select

from galaxysim.colony.labor import balanced_allocation
from galaxysim.core.space import Vec3, distance
from galaxysim.engine import intents
from galaxysim.engine.resolvers import siege
from galaxysim.engine.resolvers.production import SUPPLY_RANGE_LY
from galaxysim.engine.tick import run_ticks
from galaxysim.materials import IRON, WATER
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Building, Civ, Colony, Event, Fleet, Universe, World
from tests.conftest import (
    OUTPOST_POPULATION,
    civ_by_name,
    feed,
    give_deposits,
    home_colony,
    new_universe,
    rich_stockpile,
    take_manual_control,
)


def _outpost(session, civ, near, *, stockpile=None, name="Deep Rock"):
    """A barren world in the next system along, settled by ``civ``.

    Deliberately not in ``near``'s own system. A blockade is a cordon around a
    *system*, so two colonies sharing one star are besieged by the same fleet --
    true, and correct, and it would make every test below measure two things at
    once.
    """
    origin = near.world.system.position
    unclaimed = [
        w
        for w in session.scalars(select(World)).all()
        if w.colony is None and w.system_id != near.world.system_id
    ]
    assert unclaimed, "the test universe has no unclaimed world left"
    world = min(unclaimed, key=lambda w: (distance(origin, w.system.position), w.id))
    world.habitability = 0.0
    world.world_type = "barren"
    give_deposits(world, iron=0.02)
    colony = Colony(
        world=world,
        civ=civ,
        name=name,
        population=OUTPOST_POPULATION,
        infrastructure=1.0,
        founded_tick=0,
        stockpile=dict(stockpile or {}),
        labor=balanced_allocation(),
        management_mode="manual",
    )
    session.add(colony)
    session.flush()
    feed(colony)
    return colony


def _freighter(session, civ, colony, capacity=300_000.0):
    fleet = Fleet(
        universe_id=colony.civ.universe_id,
        civ_id=civ.id,
        name="Hauler",
        strength=0.5,
        colony_pods=0,
        speed_ly_per_hour=1.0,
        cargo={},
        cargo_capacity=capacity,
        x=colony.world.system.x,
        y=colony.world.system.y,
        z=colony.world.system.z,
    )
    session.add(fleet)
    session.flush()
    return fleet


def _besieger(session, civ, colony, *, strength, pods=0, name="Blockade Force"):
    """A hostile fleet holding station over ``colony``.

    Offset by a fifth of a light year: inside the cordon, outside the exact
    coincidence combat matches on. See this module's docstring.
    """
    fleet = Fleet(
        universe_id=colony.civ.universe_id,
        civ_id=civ.id,
        name=name,
        strength=strength,
        colony_pods=pods,
        speed_ly_per_hour=1.0,
        cargo={},
        cargo_capacity=1_000_000.0,
        x=colony.world.system.x + 0.2,
        y=colony.world.system.y,
        z=colony.world.system.z,
    )
    session.add(fleet)
    session.flush()
    return fleet


def _forward_base(session, civ, target):
    """A stocked colony of ``civ``'s within supply range of the siege.

    Not scene-setting. Fleet upkeep is billed to colonies within
    :data:`production.SUPPLY_RANGE_LY`, so a blockade parked deep in somebody
    else's space goes unsupplied and deserts within a couple of days -- which is
    the design working, and which quietly ended every long-running version of
    these tests before this helper existed. Besieging a world is something you
    have to be able to *sustain*, so the tests stage a besieger that can be.
    """
    base = _outpost(
        session, civ, target, stockpile=rich_stockpile(), name=f"{civ.name} Forward Base"
    )
    assert distance(
        base.world.system.position, target.world.system.position
    ) < SUPPLY_RANGE_LY, "the forward base must be able to supply the blockade"
    return base


def _at_war(session, aggressor, target):
    return intents.attack(session, aggressor, target.id)


# ------------------------------------------------------------------ blockade


def test_a_blockade_stops_a_supply_route_and_the_outpost_starts_dying():
    """The whole point of blockading: it is the supply line that kills.

    No new penalty is applied to the colony. Cargo simply stops moving, and the
    design's existing dependence on routes does the rest -- which is why the
    owner finds out through ``life_support_failing``, the same event they would
    see if they had cancelled the route themselves.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4101, seconds_per_tick=3600)

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        take_manual_control(session, terrans)
        home = home_colony(session, terrans)
        # Rich, and not only in water: the freighter's own upkeep is billed to
        # the colonies near it, and a hauler that cannot be supplied deserts --
        # which would end the route for a reason that is not the blockade.
        home.stockpile = rich_stockpile()
        home.world.habitability = 1.0
        outpost = _outpost(session, terrans, home, stockpile={WATER: 6_000.0})
        hauler = _freighter(session, terrans, home, capacity=60_000.0)
        intents.supply_route(
            session, terrans, hauler.id, home.id, outpost.id, {WATER: 40_000.0}
        )
        outpost_id = outpost.id

    # A week of peace: the route establishes and the outpost lives.
    run_ticks(engine, universe_id, 168)
    with open_session(engine) as session:
        supplied = session.get(Colony, outpost_id)
        assert supplied.population > 0
        assert not supplied.blockaded
        living = supplied.population

    with open_session(engine) as session:
        vex = civ_by_name(session, universe_id, "Vex")
        outpost = session.get(Colony, outpost_id)
        _forward_base(session, vex, outpost)
        _besieger(session, vex, outpost, strength=20.0)
        _at_war(session, vex, outpost.civ)
        # Spend the buffer the deliveries banked, for the reason
        # test_cutting_the_route_kills_the_colony spells out: simulating the
        # drain of months of banked air would be testing the buffer, not the cut.
        outpost.stockpile[WATER] = 1_200.0

    run_ticks(engine, universe_id, 400)

    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        assert outpost.blockaded, "a hostile fleet holding the system is a blockade"
        assert outpost.population < living * 0.5, "a cut route should be killing it"
        assert session.scalars(
            select(Event).where(Event.kind == "blockade_established")
        ).all(), "the owner must be told their supply lines were cut"
        assert session.scalars(
            select(Event).where(Event.kind == "life_support_failing")
        ).all()


def test_lifting_the_blockade_lets_the_route_run_again():
    """A standing order survives a war the player slept through.

    The route is never cancelled or failed by a blockade -- it stalls. That is
    what makes an asynchronous game survivable: the day the enemy leaves, the
    freighters move again without anybody logging in.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4102, seconds_per_tick=3600)

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        take_manual_control(session, terrans)
        home = home_colony(session, terrans)
        home.stockpile = rich_stockpile()
        home.world.habitability = 1.0
        outpost = _outpost(session, terrans, home, stockpile={WATER: 200_000.0})
        hauler = _freighter(session, terrans, home, capacity=10_000.0)
        # A manifest is the level to *keep* at the far end, so it has to be
        # above what the outpost already holds or the route has nothing to do
        # and correctly stays on the pad. The freighter's ten-thousand-tonne
        # hold is what bounds a single trip, which keeps the round trip short:
        # an outpost with no spaceport handles 250 tonnes an hour, so a bigger
        # load would spend a week unloading and hide whether the route resumed.
        intents.supply_route(
            session, terrans, hauler.id, home.id, outpost.id, {WATER: 500_000.0}
        )
        _forward_base(session, vex, outpost)
        blockader = _besieger(session, vex, outpost, strength=20.0)
        _at_war(session, vex, terrans)
        outpost_id, blockader_id = outpost.id, blockader.id

    run_ticks(engine, universe_id, 120)
    with open_session(engine) as session:
        assert session.get(Colony, outpost_id).blockaded
        assert not session.scalars(
            select(Event).where(Event.kind == "supply_delivered")
        ).all(), "nothing should have been landed while the cordon held"

    # The fleet withdraws.
    with open_session(engine) as session:
        session.delete(session.get(Fleet, blockader_id))
        withdrawal = session.get(Universe, universe_id).tick_number

    run_ticks(engine, universe_id, 120)

    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        assert not outpost.blockaded
        assert session.scalars(
            select(Event).where(Event.kind == "blockade_lifted")
        ).all(), "the owner must be told their routes are open again"
        # Deliveries, not tonnage: the outpost is bigger by now and drinks more
        # than one freighter brings, so the stockpile is the wrong thing to
        # look at. What is under test is that the order was still standing.
        assert session.scalars(
            select(Event).where(
                Event.kind == "supply_delivered", Event.tick > withdrawal
            )
        ).all(), "the standing route should have resumed on its own"


# --------------------------------------------------------------------- siege


def test_a_frontier_world_falls_and_a_homeworld_barely_notices():
    """The one balance decision in this module, checked as a property.

    Resistance is population and infrastructure and nothing else, so the same
    fleet that subdues an outpost in hours is meaningless against a capital.
    Nobody wrote that rule; it falls out of measuring what is actually there,
    and it is what stops an offline player being decapitated overnight.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4103, seconds_per_tick=3600)

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        take_manual_control(session, terrans)
        home = home_colony(session, terrans)
        home.world.habitability = 1.0
        home.stockpile = {WATER: 100_000_000.0, IRON: 1_000_000.0}
        feed(home)
        outpost = _outpost(session, terrans, home, stockpile={WATER: 10_000_000.0})

        _forward_base(session, vex, outpost)
        _besieger(session, vex, outpost, strength=50.0, name="Raiders")
        _besieger(session, vex, home, strength=50.0, name="Grand Fleet")
        _at_war(session, vex, terrans)
        home_id, outpost_id = home.id, outpost.id
        home_standing = siege.standing_resistance(home)

    run_ticks(engine, universe_id, 24)

    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        capital = session.get(Colony, home_id)

        assert outpost.resistance == 0.0, "a fifty-thousand-person outpost falls in a day"
        assert session.scalars(
            select(Event).where(Event.kind == "colony_subdued")
        ).all()

        assert capital.resistance > 0, "a homeworld does not fall to a raiding squadron"
        assert capital.resistance > home_standing * 0.99, (
            "and the same fleet should barely have scratched it -- war takes a "
            "rival's frontier, never their heart"
        )


def test_resistance_recovers_once_the_siege_lifts():
    """A world that held out gets its footing back, slowly.

    Slowly on purpose: a world that was fought over stays vulnerable for a
    while, so a second push does not have to start from nothing.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4104, seconds_per_tick=3600)

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        take_manual_control(session, terrans)
        home = home_colony(session, terrans)
        home.world.habitability = 1.0
        home.stockpile = {WATER: 100_000_000.0}
        feed(home)
        outpost = _outpost(session, terrans, home, stockpile={WATER: 10_000_000.0})
        _forward_base(session, vex, outpost)
        # A single frigate: enough to wear an outpost down over most of a day,
        # so there is something left to recover from.
        blockader = _besieger(session, vex, outpost, strength=1.0)
        _at_war(session, vex, terrans)
        outpost_id, blockader_id = outpost.id, blockader.id

    run_ticks(engine, universe_id, 6)
    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        worn = outpost.resistance
        assert 0 < worn < siege.standing_resistance(outpost), "worn but not beaten"

    with open_session(engine) as session:
        session.delete(session.get(Fleet, blockader_id))

    run_ticks(engine, universe_id, 6)
    with open_session(engine) as session:
        recovered = session.get(Colony, outpost_id).resistance
        # ``-1`` means fully recovered: back to whatever is actually there now.
        assert recovered == -1.0 or recovered > worn


# ------------------------------------------------------------------- capture


def _subdue(engine, universe_id, *, pods):
    """Stage a Terran outpost ground to zero resistance by a Vex fleet."""
    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        take_manual_control(session, terrans)
        home = home_colony(session, terrans)
        home.world.habitability = 1.0
        home.stockpile = {WATER: 100_000_000.0}
        feed(home)
        outpost = _outpost(session, terrans, home, stockpile={WATER: 10_000_000.0, IRON: 5_000.0})
        session.add(Building(colony=outpost, kind="mine", level=2.0))
        _forward_base(session, vex, outpost)
        _besieger(session, vex, outpost, strength=50.0, pods=pods)
        _at_war(session, vex, terrans)
        return outpost.id, terrans.id, vex.id


def test_grinding_a_world_down_does_not_take_it_without_a_pod():
    """Winning the siege is not the same as owning the place.

    Somebody has to land an administration. Without one the colony simply stays
    subdued and blockaded -- which is a perfectly good thing to want, and the
    reason starving a rival is a strategy rather than an oversight.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4105, seconds_per_tick=3600)
    outpost_id, terran_id, _ = _subdue(engine, universe_id, pods=0)

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        assert outpost.resistance == 0.0, "it should be beaten"
        assert outpost.civ_id == terran_id, "but not annexed"
        assert outpost.blockaded, "and still cut off"
        assert not session.scalars(
            select(Event).where(Event.kind == "colony_captured")
        ).all()


def test_a_pod_takes_the_world_with_its_industry_and_its_warehouses():
    """Why capturing beats founding: the prize is a *developed* world."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4106, seconds_per_tick=3600)
    outpost_id, terran_id, vex_id = _subdue(engine, universe_id, pods=1)

    with open_session(engine) as session:
        iron_before = session.get(Colony, outpost_id).stockpile.get(IRON, 0.0)

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        colony = session.get(Colony, outpost_id)
        assert colony.civ_id == vex_id, "the pod landed and the world changed hands"
        assert colony.civ.id == vex_id, (
            "and the session must agree with the row -- every resolver after "
            "the siege reads objects, not columns"
        )
        assert colony.world.colony is colony, "the world still points at its colony"

        assert any(b.kind == "mine" for b in colony.buildings), "the mine came with it"
        assert colony.stockpile.get(IRON, 0.0) >= iron_before, "so did the warehouse"
        assert colony.population > 0, "and it is still a place people live"

        lander = session.scalar(
            select(Fleet).where(Fleet.civ_id == vex_id, Fleet.name == "Blockade Force")
        )
        assert lander.colony_pods == 0, "the pod was spent"

        captured = session.scalars(
            select(Event).where(Event.kind == "colony_captured")
        ).all()
        assert {e.civ_id for e in captured} == {terran_id, vex_id}, (
            "both sides are told"
        )
        # Read off the event rather than by comparing populations: a captured
        # world goes on living, and by the end of the run it has grown back past
        # where it started.
        assert all(e.payload["population_lost"] > 0 for e in captured), (
            "people are lost in the taking"
        )


def test_a_captured_world_is_not_still_at_war_with_itself():
    """After the flag changes, the siege is over: it is home now."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4107, seconds_per_tick=3600)
    outpost_id, _, vex_id = _subdue(engine, universe_id, pods=1)

    run_ticks(engine, universe_id, 72)

    with open_session(engine) as session:
        colony = session.get(Colony, outpost_id)
        assert colony.civ_id == vex_id
        assert not colony.blockaded, "its own fleet is not besieging it"
        assert colony.resistance == -1.0, "and it holds out in full again"
        assert not session.scalars(
            select(Event).where(
                Event.kind == "blockade_lifted", Event.civ_id == vex_id
            )
        ).all(), "no spurious lift announced to the civ that imposed it"


# ----------------------------------------------------------------- neutrality


def test_a_fleet_passing_through_is_not_a_blockade():
    """Hostility is declared. Sitting in someone's system is not a war."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4108, seconds_per_tick=3600)

    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        take_manual_control(session, terrans)
        home = home_colony(session, terrans)
        home.world.habitability = 1.0
        home.stockpile = {WATER: 100_000_000.0}
        feed(home)
        outpost = _outpost(session, terrans, home, stockpile={WATER: 10_000_000.0})
        _besieger(session, vex, outpost, strength=500.0)
        outpost_id = outpost.id
        # No attack order.

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        outpost = session.get(Colony, outpost_id)
        assert not outpost.blockaded
        assert outpost.resistance == -1.0, "nothing was ground down"


# ------------------------------------------------------ the AI taking a world


def _ai_besieging(engine, universe_id, *, pods):
    """A Vex AI with warships over a Terran outpost, at war, ready to land.

    Staged as a *position* rather than as orders: the AI is then asked what it
    wants to do about it, which is the thing under test.
    """
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        universe.ai_difficulty = "driven"
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        vex.is_ai = True
        take_manual_control(session, terrans)

        home = home_colony(session, terrans)
        home.world.habitability = 1.0
        home.stockpile = rich_stockpile()
        feed(home)
        outpost = _outpost(session, terrans, home, stockpile={WATER: 10_000_000.0})
        base = _forward_base(session, vex, outpost)
        base.stockpile = rich_stockpile()

        besieger = _besieger(session, vex, outpost, strength=50.0)
        # The pod sits at the Vex base, not at the siege -- exactly the position
        # a civilization is in after its fleet arrives ahead of its settlers.
        lander = Fleet(
            universe_id=universe_id,
            civ_id=vex.id,
            name="Lander",
            strength=1.0,
            colony_pods=pods,
            speed_ly_per_hour=1.0,
            cargo={},
            cargo_capacity=40.0,
            x=base.world.system.x,
            y=base.world.system.y,
            z=base.world.system.z,
        )
        session.add(lander)
        _at_war(session, vex, terrans)
        session.flush()
        return outpost.id, vex.id, besieger.id, lander.id


def test_an_ai_holding_a_blockade_sends_somebody_to_take_the_world():
    """The half of conflict that never happened once.

    Blockade worked and siege worked -- two colonies were cut off and ground to
    nothing in a sixty-day soak -- and then the war simply stopped, because a
    colony pod was dispatched at the moment war was declared and never again.
    One of those worlds sat subdued and blockaded for three hundred and thirteen
    hours with nobody coming for it.

    Wanting the world has to be a standing intention, so this asks the AI what
    it wants to do about a siege it is already holding, and expects it to send
    somebody.
    """
    from galaxysim.ai.simple import take_turn

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4201, seconds_per_tick=3600)
    outpost_id, vex_id, _, lander_id = _ai_besieging(engine, universe_id, pods=1)

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        take_turn(session, universe, session.get(Civ, vex_id))
        session.flush()
        # An order, not a position: queuing a move does not move anything until
        # the movement resolver runs on the tick.
        from galaxysim.model.entities import Intent, IntentKind

        # Toward the besieged world specifically. Asserting merely that *a*
        # move order exists is a false positive: expansion issues one too, for
        # a rock on the other side of the map, and an earlier version of this
        # test passed on exactly that.
        besieged = session.get(Colony, outpost_id).world.system.position
        ordered = [
            order
            for order in session.scalars(select(Intent).where(Intent.civ_id == vex_id))
            if order.kind == IntentKind.MOVE_FLEET.value
            and distance(
                Vec3(order.payload["x"], order.payload["y"], order.payload["z"]),
                besieged,
            )
            <= siege.BLOCKADE_RANGE_LY
        ]
        assert ordered, "somebody should have been ordered to the siege to land"
        carrier = session.get(Fleet, ordered[0].payload["fleet_id"])
        assert carrier.colony_pods > 0, "and it has to be carrying a pod"

    run_ticks(engine, universe_id, 96)

    with open_session(engine) as session:
        colony = session.get(Colony, outpost_id)
        assert colony.civ_id == vex_id, (
            "the siege should have ended in the world changing hands"
        )
        assert session.scalars(
            select(Event).where(Event.kind == "colony_captured")
        ).all()


def test_a_pod_already_settling_a_world_is_not_also_sent_to_a_siege():
    """One ship, one job.

    Every ``_maybe_*`` decision works out which ships are free from a snapshot
    of pending orders taken before any of them ran, so an order issued by an
    earlier decision was invisible to a later one. Expansion dispatches every
    idle colony ship -- and a queued move does not set ``in_transit``, so the
    ship still looked idle -- and the war then picked the same hull as the pod
    it meant to land. The colonisation won every time.
    """
    from galaxysim.ai.simple import _Turn, _maybe_annex, _maybe_expand
    from galaxysim.model.entities import Intent

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=4202, seconds_per_tick=3600)
    _, vex_id, _, lander_id = _ai_besieging(engine, universe_id, pods=1)

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        civ = session.get(Civ, vex_id)
        turn = _Turn(session, universe, civ)
        pending = {}

        _maybe_expand(turn, pending)
        session.flush()
        after_expansion = len(
            [
                order
                for order in session.scalars(select(Intent).where(Intent.civ_id == vex_id))
                if order.payload.get("fleet_id") == lander_id
            ]
        )
        assert after_expansion, "the fixture needs expansion to claim the lander first"

        _maybe_annex(turn, pending)
        session.flush()
        after_annex = len(
            [
                order
                for order in session.scalars(select(Intent).where(Intent.civ_id == vex_id))
                if order.payload.get("fleet_id") == lander_id
            ]
        )

        assert after_annex == after_expansion, (
            "the siege took a ship expansion had already sent somewhere else; "
            "one hull cannot settle a rock and land on a besieged world at once"
        )
