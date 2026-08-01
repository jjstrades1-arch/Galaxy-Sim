"""Societies at real scale.

The properties here are the ones the phase exists to produce, and each is a
statement about the *game* rather than about a function:

* A homeworld is already full, so growth has to come from expanding.
* A dead world is an outpost forever, whatever you do short of terraforming.
* A world with an edible biosphere feeds itself; one without pays for it.
* Industry is bounded by people and ground, both of which move.
* Shipping people beats waiting for them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.colony.agriculture import quality, regime
from galaxysim.colony.industry import (
    KM2_PER_LEVEL,
    STAFF_PER_LEVEL,
    binding_limit,
    cost_of_level,
    development,
    effect_scale,
    max_total_levels,
    productivity,
    work_of_level,
)
from galaxysim.colony.labor import AGRICULTURE, SECTORS, balanced_allocation
from galaxysim.colony.population import (
    HABITAT_CAPACITY_PER_INFRASTRUCTURE,
    capacity,
    growth_per_hour,
    habitat_capacity,
    natural_capacity,
)
from galaxysim.core.units import format_count
from galaxysim.engine import intents
from galaxysim.engine.rates import DEFAULT_RATES
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Colony, Event, Fleet, IntentStatus, World
from galaxysim.worldgen.serialize import survey_from_json
from tests.conftest import (
    civ_by_name,
    feed,
    give_deposits,
    home_colony,
    make_farmable,
    new_universe,
    rich_stockpile,
)


# --- capacity ----------------------------------------------------------------


class _World:
    """Just enough of a World for the capacity arithmetic."""

    def __init__(self, habitability: float, carrying_capacity: float) -> None:
        self.habitability = habitability
        self.carrying_capacity = carrying_capacity


def test_a_living_world_is_limited_by_its_land():
    world = _World(habitability=0.8, carrying_capacity=9.0e9)
    # Habitats are irrelevant next to a continent's worth of open ground.
    assert capacity(world, infrastructure=50.0) == pytest.approx(9.0e9)
    assert natural_capacity(world) == pytest.approx(9.0e9)


def test_a_dead_world_is_an_outpost_however_long_you_wait():
    """The payoff terraforming exists to unlock.

    Natural capacity on a dead world is genuinely zero -- nobody lives outdoors
    on Mars -- so the only ceiling is what the habitats hold. That is three
    orders of magnitude below a real world, and no amount of time, ore or
    industry moves it. Only changing the planet does.
    """
    dead = _World(habitability=0.0, carrying_capacity=0.0)
    assert natural_capacity(dead) == 0.0

    bare = capacity(dead, infrastructure=1.0)
    built = capacity(dead, infrastructure=40.0)
    assert bare == pytest.approx(HABITAT_CAPACITY_PER_INFRASTRUCTURE)
    assert built > bare, "habitats do raise it"
    assert built < 1e7, "but never within three orders of a real world"


def test_terraforming_switches_which_ceiling_binds():
    """Raise habitability and the artificial floor stops being the answer.

    An outpost of a few hundred thousand becomes a world of billions -- a
    thousandfold transformation from one change to the planet, which is what
    makes reshaping a world worth a mature civilization's surplus.
    """
    before = capacity(_World(habitability=0.0, carrying_capacity=0.0), infrastructure=20.0)
    after = capacity(_World(habitability=0.7, carrying_capacity=8.0e9), infrastructure=20.0)
    assert after / before > 1_000


def test_growth_is_logistic_and_reverses_when_hungry():
    """One formula, three behaviours, no special cases."""
    rate = DEFAULT_RATES.population_growth_per_hour

    # Room to grow.
    assert growth_per_hour(1e6, 1e10, base_rate=rate) > 0
    # Nearly full: the same colony barely moves, *as a fraction of itself* --
    # which is the sense in which a homeworld "barely grows".
    frontier = growth_per_hour(1e6, 1e10, base_rate=rate) / 1e6
    settled = growth_per_hour(9.9e9, 1e10, base_rate=rate) / 9.9e9
    assert settled < frontier / 50
    # Over capacity: it comes back down.
    assert growth_per_hour(1.2e10, 1e10, base_rate=rate) < 0
    # Starving: negative standard of living reverses it even with headroom.
    assert growth_per_hour(1e6, 1e10, base_rate=rate, standard_of_living=-0.5) < 0
    # Nowhere to live at all.
    assert growth_per_hour(1e6, 0.0, base_rate=rate) < 0


# --- industry ----------------------------------------------------------------


def test_levels_cost_more_and_return_less():
    """The curve that makes expansion beat deepening.

    Cost is quadratic in level and effect is a square root, so the marginal
    return on the next level falls away sharply. Past a point a civilization is
    better off founding a new colony -- and nothing had to say so.
    """
    first = cost_of_level({"steel": 30.0}, 1)["steel"]
    tenth = cost_of_level({"steel": 30.0}, 10)["steel"]
    assert tenth == pytest.approx(first * 100)
    assert work_of_level(8.0, 10) == pytest.approx(work_of_level(8.0, 1) * 100)

    assert effect_scale(100) == pytest.approx(10.0)
    assert effect_scale(1) == pytest.approx(1.0)

    # Return per tonne invested falls monotonically.
    def value_per_cost(level: int) -> float:
        total = sum(cost_of_level({"steel": 30.0}, n)["steel"] for n in range(1, level + 1))
        return effect_scale(level) / total

    assert value_per_cost(1) > value_per_cost(10) > value_per_cost(100)


def test_productivity_is_bounded_however_much_is_built():
    """Development helps, and there is a limit to how much.

    This bound is load-bearing: the industries that raise development also raise
    their own sector bonuses, and letting both grow freely made a capital's
    research run a hundred times faster than intended.
    """
    assert productivity(0.0) < productivity(0.5) < productivity(1.0)
    assert productivity(5.0) == productivity(1.0), "clamped above full development"
    assert productivity(0.0) > 0, "an unbuilt colony still works"
    assert productivity(1.0) < 10.0


def test_development_is_scale_free():
    """Half-built is half-built whether the colony is a town or a world."""
    assert development(50, 100) == pytest.approx(0.5)
    assert development(5_000, 10_000) == pytest.approx(0.5)
    assert development(200, 100) == 1.0, "clamped"
    assert development(1, 0) == 0.0


def test_both_physical_limits_bind_and_both_rise():
    crowded_moon = max_total_levels(population=1e10, land_area_km2=10 * KM2_PER_LEVEL)
    empty_continent = max_total_levels(population=10 * STAFF_PER_LEVEL, land_area_km2=1e9)
    assert crowded_moon == 10 and binding_limit(1e10, 10 * KM2_PER_LEVEL) == "ground"
    assert empty_continent == 10 and binding_limit(10 * STAFF_PER_LEVEL, 1e9) == "people"

    # Both move as the colony develops, which is what makes growth compound.
    assert max_total_levels(20 * STAFF_PER_LEVEL, 1e9) > empty_continent


# --- agriculture -------------------------------------------------------------


def _survey_of(session, **shape):
    world = session.scalars(select(World).order_by(World.id)).first()
    if shape.get("farmable"):
        make_farmable(world)
    return survey_from_json(world.survey)


def test_an_edible_biosphere_is_worth_a_tenfold_difference(engine):
    """The concrete payoff for finding life you can eat.

    Habitability does not capture this: a world can be perfectly breathable and
    still a terrible farm, and a colony that has to manufacture every calorie
    puts a third of its people into hydroponics instead of into mines.
    """
    universe_id = new_universe(engine, civs=("Terrans",))
    with open_session(engine) as session:
        garden = _survey_of(session, farmable=True)
        assert regime(garden) == "open farmland"
        assert quality(garden) > 1.0

    # The same world stripped of its oceans and its ecology.
    with open_session(engine) as session:
        world = session.scalars(select(World).order_by(World.id)).first()
        survey = dict(world.survey)
        survey["hydrosphere"] = dict(survey["hydrosphere"], liquid_water=False)
        survey["biosphere"] = dict(survey["biosphere"], stage="sterile", biochemistry=None)
        world.survey = survey
        rock = survey_from_json(world.survey)

    assert regime(rock) == "hydroponics"
    assert quality(garden) / quality(rock) > 8.0


def test_a_colony_with_no_food_stops_growing_and_then_shrinks(engine):
    """Standard of living is what carries "is this working" into the numbers."""
    universe_id = new_universe(engine, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        colony.management_mode = "manual"
        # Nobody farming, no larder, and the world cannot feed them.
        intents.set_labor(session, colony, {AGRICULTURE: 0.0, "extraction": 1.0})
        colony.stockpile = {}
        colony.population = 1_000_000.0
        colony.world.carrying_capacity = 1e10  # plenty of room, so only food binds
        colony_id, before = colony.id, colony.population

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.standard_of_living < 0.5, "nobody is being fed"
        assert colony.population < before, "and a hungry colony shrinks"


def test_agriculture_is_a_real_sector():
    assert AGRICULTURE in SECTORS
    assert balanced_allocation()[AGRICULTURE] == pytest.approx(1 / len(SECTORS))


# --- migration ---------------------------------------------------------------


def _seed_settlement(engine, *, migrate_people: float) -> float:
    """Plant a young colony, optionally ship settlers to it, return its size.

    Same seed and same length of run either way, so the only difference between
    two calls is whether a convoy was ordered.
    """
    universe_id = new_universe(engine, seed=6161, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.management_mode = "manual"

        world = next(
            w for w in session.scalars(select(World).order_by(World.id)) if w.colony is None
        )
        world.habitability = 0.8
        world.carrying_capacity = 5.0e9
        make_farmable(world)
        give_deposits(world, iron=0.02, water_ice=0.02)
        settlement = Colony(
            world_id=world.id,
            civ_id=civ.id,
            name="Landfall",
            population=200_000.0,
            infrastructure=1.0,
            founded_tick=0,
            stockpile=rich_stockpile(),
            labor=balanced_allocation(),
        )
        settlement.management_mode = "manual"
        session.add(settlement)
        session.flush()
        feed(settlement)
        settlement_id = settlement.id

        if migrate_people:
            fleet = Fleet(
                universe_id=home.civ.universe_id,
                civ_id=civ.id,
                name="Convoy",
                strength=1.0,
                colony_pods=0,
                speed_ly_per_hour=1.0,
                cargo={},
                cargo_capacity=migrate_people,
                x=home.world.system.x,
                y=home.world.system.y,
                z=home.world.system.z,
            )
            session.add(fleet)
            session.flush()
            intents.migrate(session, civ, fleet.id, home.id, settlement_id, migrate_people)

    run_ticks(engine, universe_id, 300)

    with open_session(engine) as session:
        return session.get(Colony, settlement_id).population


def test_shipping_people_beats_waiting_for_them():
    """The strategic act the design is built around.

    Natural growth carries a colony on its own, so migration is never required.
    What it buys is *time*: the same colony over the same three hundred hours is
    measurably bigger when settlers were shipped in, because they arrive as
    population rather than compounding from a smaller base.

    Note what is *not* asserted -- that the capital shrank. Moving two million
    people off a world of fourteen billion is a rounding error there, which is
    itself the point: the capital is a reservoir, and drawing on it costs it
    almost nothing.
    """
    unaided = _seed_settlement(create_engine_for("sqlite://"), migrate_people=0.0)
    shipped = _seed_settlement(create_engine_for("sqlite://"), migrate_people=2_000_000.0)

    assert shipped > unaided * 1.5, (
        f"shipping two million settlers should tell: {shipped:,.0f} vs {unaided:,.0f}"
    )


def test_a_convoy_lands_its_people_and_says_so():
    engine = create_engine_for("sqlite://")
    _seed_settlement(engine, migrate_people=2_000_000.0)
    with open_session(engine) as session:
        assert session.scalars(
            select(Event).where(Event.kind == "migrants_landed")
        ).all(), "an arrival is worth a line in the log"


def test_migrants_ride_in_the_hold_they_would_have_carried_ore_in(engine):
    """Moving people costs the cargo run you did not make."""
    universe_id = new_universe(engine, civs=("Terrans",))
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        fleet = Fleet(
            universe_id=home.civ.universe_id,
            civ_id=civ.id,
            name="Hauler",
            strength=1.0,
            speed_ly_per_hour=1.0,
            cargo={},
            cargo_capacity=1_000.0,
            x=home.world.system.x,
            y=home.world.system.y,
            z=home.world.system.z,
        )
        session.add(fleet)
        session.flush()

        assert fleet.cargo_space == pytest.approx(1_000.0)
        fleet.passengers = 1_000.0
        expected = 1_000.0 * DEFAULT_RATES.tonnes_per_passenger
        assert fleet.passenger_tonnage == pytest.approx(expected)
        assert fleet.cargo_space == pytest.approx(1_000.0 - expected)


def test_migration_between_other_peoples_colonies_is_rejected(engine):
    universe_id = new_universe(engine, civs=("Terrans", "Vex"), seconds_per_tick=3600)
    with open_session(engine) as session:
        terrans = civ_by_name(session, universe_id, "Terrans")
        vex = civ_by_name(session, universe_id, "Vex")
        home = home_colony(session, terrans)
        theirs = home_colony(session, vex)
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == terrans.id).order_by(Fleet.id))
        intents.migrate(session, terrans, fleet.id, home.id, theirs.id, 1000.0)

    run_ticks(engine, universe_id, 2)

    with open_session(engine) as session:
        intent = session.scalar(
            select(intents.Intent).where(intents.Intent.kind == "migrate")
        )
        assert intent.status == IntentStatus.FAILED.value
        assert "your own" in intent.result


def test_migration_needs_somewhere_to_go_and_someone_to_move():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, civs=("Terrans",))
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        with pytest.raises(ValueError):
            intents.migrate(session, civ, 1, 1, 1, 100.0)
        with pytest.raises(ValueError):
            intents.migrate(session, civ, 1, 1, 2, 0.0)


# --- readability -------------------------------------------------------------


def test_magnitudes_are_readable():
    """Nothing at this scale is legible as a raw number."""
    assert format_count(18_145_461_021) == "18.15B"
    assert format_count(6_150_000) == "6.15M"
    assert format_count(50_000) == "50.0k"
    assert format_count(27) == "27"


# --- what the AI does with all this ------------------------------------------


def test_the_ai_ships_settlers_when_there_is_somewhere_to_put_them(engine):
    """The AI uses the same orders a player does, including convoys.

    Worth stating what this does *not* prove: that the AI migrates in an
    ordinary game. It mostly cannot, because every world it can reach caps at
    its habitat ceiling and fills to it on its own — there is nowhere for
    settlers to go until terraforming raises a ceiling. This checks the AI
    reaches for the order when the room exists, so that terraforming turns it on
    rather than requiring new AI code.
    """
    from galaxysim.ai import simple

    universe_id = new_universe(engine, seed=4141, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        civ.is_ai = True
        home = home_colony(session, civ)

        # A second world with genuine room: habitable, and nearly empty.
        world = next(
            w for w in session.scalars(select(World).order_by(World.id)) if w.colony is None
        )
        world.habitability = 0.8
        world.carrying_capacity = 5.0e9
        make_farmable(world)
        give_deposits(world, iron=0.02, water_ice=0.02)
        roomy = Colony(
            world_id=world.id,
            civ_id=civ.id,
            name="Roomy",
            population=100_000.0,
            infrastructure=1.0,
            founded_tick=0,
            stockpile=rich_stockpile(),
            labor=balanced_allocation(),
        )
        session.add(roomy)
        session.flush()
        feed(roomy)

        # And a hull big enough to carry a convoy.
        session.add(
            Fleet(
                universe_id=home.civ.universe_id,
                civ_id=civ.id,
                name="Hauler",
                strength=0.5,
                speed_ly_per_hour=1.0,
                cargo={},
                cargo_capacity=simple.MIGRATION_BATCH * simple.TONNES_PER_SETTLER,
                x=home.world.system.x,
                y=home.world.system.y,
                z=home.world.system.z,
            )
        )
        session.flush()
        universe = home.civ.universe
        simple.take_turn(session, universe, civ)

        order = session.scalar(
            select(intents.Intent).where(intents.Intent.kind == "migrate")
        )
        assert order is not None, "the AI should reach for a convoy when there is room"
        assert order.payload["dest_colony_id"] == roomy.id
        assert order.payload["people"] > 0


def test_a_fleet_draws_on_whatever_its_civ_has_nearby(engine):
    """Upkeep is a question about the neighbourhood, not the nearest rock.

    A fleet parked over a two-week-old outpost is not unsupplied because that
    outpost has no fuel — it is supplied from the developed world one jump
    behind it. Billing only the closest colony meant a frontier fleet bled
    continuously with a full warehouse four light-years away.
    """
    from galaxysim.materials import FUEL

    universe_id = new_universe(engine, seed=4242, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.management_mode = "manual"

        # An empty outpost, closer to the fleet than the capital is.
        world = next(
            w for w in session.scalars(select(World).order_by(World.id)) if w.colony is None
        )
        give_deposits(world)
        broke = Colony(
            world_id=world.id,
            civ_id=civ.id,
            name="Broke",
            population=50_000.0,
            infrastructure=1.0,
            founded_tick=0,
            stockpile={},
            labor=balanced_allocation(),
        )
        broke.management_mode = "manual"
        session.add(broke)
        session.flush()
        feed(broke)

        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        fleet.x, fleet.y, fleet.z = (
            world.system.x,
            world.system.y,
            world.system.z,
        )
        fleet_id, strength_before = fleet.id, fleet.strength
        home.stockpile[FUEL] = 1e9

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        assert session.get(Fleet, fleet_id).strength == pytest.approx(strength_before), (
            "the capital's fuel should have covered it"
        )
        assert not session.scalars(
            select(Event).where(Event.kind == "upkeep_shortfall")
        ).all(), "and nothing should have been logged as short"
