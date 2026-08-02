"""What a tick is allowed to cost.

Performance is a design property here, not an optimisation detail. This is an
async game: a shared universe ticks on a wall clock for everybody at once, so a
tick that takes a second at three hundred colonies does not merely feel slow, it
puts a ceiling on how big the game can be.

The failure mode these tests exist to catch is specific and it has happened
twice: a resolver asks a question *inside a loop*. It reads correctly, it passes
every other test, and it turns a tick's cost from "proportional to what happened"
into "proportional to the size of the universe times the number of civilizations
in it". At a hundred and twenty colonies that was six hundred milliseconds a
tick, nearly all of it SQL.

So the property under test is not a millisecond budget -- those vary by machine
and would rot. It is **the shape of the curve**: the number of database round
trips a tick makes must not grow with the number of colonies.
"""

from __future__ import annotations

from sqlalchemy import event, select

from galaxysim.ai import take_all_turns
from galaxysim.colony.labor import balanced_allocation
from galaxysim.engine import intents
from galaxysim.engine.tick import resolve_tick, run_ticks
from galaxysim.materials import FOOD, WATER
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Building, Civ, Colony, Fleet, Universe, World
from tests.conftest import give_deposits, new_universe, rich_stockpile


def _populate(engine, universe_id: int, civs: int, colonies_each: int) -> None:
    """Give every civ a spread of developed colonies and a few ships."""
    with open_session(engine) as session:
        free = [
            w for w in session.scalars(select(World).order_by(World.id)) if w.colony is None
        ]
        index = 0
        for civ in session.scalars(select(Civ).order_by(Civ.id)):
            home = session.scalar(select(Colony).where(Colony.civ_id == civ.id))
            for _ in range(colonies_each - 1):
                world = free[index]
                index += 1
                world.habitability = 0.5
                give_deposits(world, iron=0.02, silicon=0.02, water_ice=0.01, carbon=0.01)
                colony = Colony(
                    world_id=world.id,
                    civ_id=civ.id,
                    name=f"c{index}",
                    population=5.0e6,
                    infrastructure=4.0,
                    founded_tick=0,
                    stockpile=rich_stockpile(1.0e7),
                    labor=balanced_allocation(),
                )
                session.add(colony)
                session.flush()
                for kind in ("mine", "factory", "spaceport", "laboratory"):
                    session.add(
                        Building(
                            colony_id=colony.id,
                            kind=kind,
                            level=6,
                            work_remaining=0.0,
                            completed_tick=0,
                        )
                    )
            for f in range(3):
                session.add(
                    Fleet(
                        universe_id=universe_id,
                        civ_id=civ.id,
                        name=f"f{f}",
                        strength=2.0,
                        speed_ly_per_hour=1.0,
                        cargo={},
                        cargo_capacity=500.0,
                        x=home.world.system.x,
                        y=home.world.system.y,
                        z=home.world.system.z,
                    )
                )


def _route_everything(engine, universe_id: int) -> None:
    """Give every colony but the first a standing route from the first.

    Routes were the single largest thing this file could not see. The fixture
    never made one, so ``_resolve_routes`` -- which took three point lookups per
    route per tick and turned out to be a third of a real tick -- ran zero times
    in every measurement here. A guard that does not exercise the code cannot
    guard it, and this is the shape of that mistake.
    """
    with open_session(engine) as session:
        for civ in session.scalars(select(Civ).order_by(Civ.id)):
            colonies = session.scalars(
                select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id)
            ).all()
            if len(colonies) < 2:
                continue
            source = colonies[0]
            for destination in colonies[1:]:
                freighter = Fleet(
                    universe_id=universe_id,
                    civ_id=civ.id,
                    name=f"hauler{destination.id}",
                    strength=0.5,
                    speed_ly_per_hour=1.0,
                    cargo={},
                    cargo_capacity=5_000.0,
                    x=source.world.system.x,
                    y=source.world.system.y,
                    z=source.world.system.z,
                )
                session.add(freighter)
                session.flush()
                intents.supply_route(
                    session,
                    civ,
                    freighter.id,
                    source.id,
                    destination.id,
                    {WATER: 500.0, FOOD: 200.0},
                )


def _advance(engine, universe_id: int, ticks: int, *, with_ai: bool) -> None:
    """Resolve ticks, optionally letting the AI take its turn first.

    ``run_ticks`` alone is only half a tick in a real game -- the other half is
    every civilization deciding what to do, which is where two of the three
    worst offenders were hiding.
    """
    if not with_ai:
        run_ticks(engine, universe_id, ticks)
        return
    for _ in range(ticks):
        with open_session(engine) as session:
            universe = session.get(Universe, universe_id)
            take_all_turns(session, universe)
            session.flush()
            resolve_tick(session, universe)


def _statements_per_tick(
    civs: int, colonies_each: int, *, with_ai: bool = False, routes: bool = True
) -> float:
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine,
        seed=7,
        civs=tuple(f"C{i}" for i in range(civs)),
        system_count=90,
        seconds_per_tick=3600,
        ai=with_ai,
    )
    _populate(engine, universe_id, civs, colonies_each)
    if routes:
        _route_everything(engine, universe_id)
    _advance(engine, universe_id, 2, with_ai=with_ai)  # settle

    counted = 0

    def count(conn, cursor, statement, parameters, context, executemany):
        nonlocal counted
        counted += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        _advance(engine, universe_id, 5, with_ai=with_ai)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return counted / 5


def test_a_tick_does_not_query_more_as_the_game_grows():
    """The property, stated plainly.

    Ten colonies and eighty colonies must cost about the same number of round
    trips. If this fails, something is asking the database a question inside a
    loop over colonies -- and the fix is to hoist the query out and group the
    result, not to raise the bound here.
    """
    small = _statements_per_tick(civs=2, colonies_each=5)
    large = _statements_per_tick(civs=4, colonies_each=20)

    assert large < small * 1.6, (
        f"queries per tick grew from {small:.0f} at 10 colonies to {large:.0f} at 80. "
        "Something is querying inside a loop over colonies."
    )


def test_a_tick_does_not_query_more_as_players_join():
    """The other axis, and the one the colony test is too coarse to see.

    Same number of colonies, four times as many civilizations. A resolver that
    reads the whole intent table or the whole colony table *once per civ* adds
    only a handful of queries at four civs, so it hides behind the noise above --
    and then costs a hundred extra round trips a tick in a shared universe with
    a hundred players, which is precisely where it matters most.
    """
    few = _statements_per_tick(civs=2, colonies_each=21)
    many = _statements_per_tick(civs=8, colonies_each=6)

    assert many < few * 1.5, (
        f"queries per tick grew from {few:.0f} with 2 civs to {many:.0f} with 8, "
        "at the same colony count. Something is querying per civilization."
    )


def test_a_tick_is_a_bounded_number_of_round_trips():
    """An absolute ceiling as well as a shape.

    Deliberately loose -- this is a guard rail, not a budget. It exists so that
    adding a resolver which reads the whole world twice per civilization is
    noticed here rather than in a soak run three phases later.
    """
    assert _statements_per_tick(civs=4, colonies_each=20) < 120


def test_supply_routes_do_not_cost_a_query_each():
    """The third of a tick this file could not see.

    ``_resolve_routes`` took three point lookups per route per tick -- the
    fleet, the origin and the destination -- so a civilization supplying thirty
    outposts paid ninety round trips an hour to move nothing new. It never
    showed up here because the fixture had no routes in it at all.
    """
    without = _statements_per_tick(civs=2, colonies_each=10, routes=False)
    with_routes = _statements_per_tick(civs=2, colonies_each=10, routes=True)

    assert with_routes < without * 1.5, (
        f"adding a route to every colony took a tick from {without:.0f} queries "
        f"to {with_routes:.0f}. Routes are being resolved one lookup at a time."
    )


def test_the_ai_does_not_query_more_as_its_empire_grows():
    """The other half of a tick, and the half that was never measured.

    Deciding what to do is as much of a real tick as resolving it -- at a
    hundred colonies it was very nearly half -- and every guard in this file ran
    against a universe where nobody decided anything.
    """
    small = _statements_per_tick(civs=2, colonies_each=5, with_ai=True)
    large = _statements_per_tick(civs=4, colonies_each=20, with_ai=True)

    assert large < small * 1.8, (
        f"queries per tick grew from {small:.0f} at 10 colonies to {large:.0f} "
        "at 80 once the AI was taking its turn. Something in the AI is asking "
        "the database a question per colony, per world, or per candidate system."
    )


def test_the_survey_document_is_not_read_by_the_ai_either():
    """The guard below, applied to the code that walked straight past it.

    A world's survey is the largest document in the game and the tick loop is
    forbidden from touching it -- but the AI was never part of "the tick loop"
    as this file defined it, and quietly parsed the whole thing per candidate
    world per turn while deciding what to terraform.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=7, civs=("A", "B"), seconds_per_tick=3600, ai=True
    )
    _populate(engine, universe_id, civs=2, colonies_each=6)
    _advance(engine, universe_id, 2, with_ai=True)

    surveys = 0

    def watch(conn, cursor, statement, parameters, context, executemany):
        nonlocal surveys
        if "worlds.survey" in statement:
            surveys += 1

    event.listen(engine, "before_cursor_execute", watch)
    try:
        _advance(engine, universe_id, 5, with_ai=True)
    finally:
        event.remove(engine, "before_cursor_execute", watch)

    assert surveys == 0, (
        f"the survey document was fetched {surveys} times in five AI turns; "
        "everything the AI needs from it should be a promoted column"
    )


def test_the_survey_document_is_not_read_during_a_tick():
    """The largest JSON blob in the game must stay out of the hot path.

    A world's survey is a big document and the session is per tick, so every
    load decodes it again. Everything the tick loop needs from it lives in
    promoted columns; if a resolver starts reading ``world.survey`` directly it
    will be loaded for every colony every tick, and this catches that.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=7, civs=("A", "B"), system_count=40, seconds_per_tick=3600
    )
    _populate(engine, universe_id, civs=2, colonies_each=6)
    run_ticks(engine, universe_id, 2)

    surveys = 0

    def watch(conn, cursor, statement, parameters, context, executemany):
        nonlocal surveys
        if "worlds.survey" in statement:
            surveys += 1

    event.listen(engine, "before_cursor_execute", watch)
    try:
        run_ticks(engine, universe_id, 5)
    finally:
        event.remove(engine, "before_cursor_execute", watch)

    assert surveys == 0, (
        f"the survey document was fetched {surveys} times in five ticks; "
        "the tick loop should read the promoted columns instead"
    )
