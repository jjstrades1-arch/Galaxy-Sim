"""The colony interior: labor, life support and buildings.

The property most of these defend is that a colony is a *place with
constraints*, not a number that goes up. Labor is finite, industry is bounded by
the people and the ground available to it, and a hostile world takes a real bite
out of all of them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from galaxysim.colony.buildings import building_type
from galaxysim.colony.industry import (
    KM2_PER_LEVEL,
    cost_of_level,
    binding_limit,
    levels_in_use,
    max_total_levels,
)
from galaxysim.colony.expedition import Loadout, assess
from galaxysim.colony.labor import (
    EXTRACTION,
    INDUSTRY,
    LIFE_SUPPORT,
    RESEARCH,
    SECTORS,
    balanced_allocation,
    normalize,
)
from galaxysim.materials import (
    CERAMICS,
    CONSTRUCTION,
    COPPER,
    ELECTRONICS,
    FOOD,
    IRON,
    MATERIALS,
    RARE_EARTHS,
    STEEL,
    WATER,
    can_afford,
)

#: A warehouse and a crust that between them cannot make electronics.
#:
#: Zeroing the finished good is not enough and that is the whole point of the
#: chain: a colony holding copper and rare earths refines its own within the
#: hour, so a test that only empties the ELECTRONICS line measures nothing. This
#: is the position ``AI-5`` was actually in -- no copper and no rare earths in
#: the ground, and none in the warehouse either.
NO_ELECTRONICS_CHAIN = (ELECTRONICS, COPPER, RARE_EARTHS)
from galaxysim.engine import intents
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import (
    Building,
    Colony,
    Event,
    Fleet,
    Intent,
    IntentKind,
    IntentStatus,
    World,
)
from tests.conftest import (
    civ_by_name,
    clone_world,
    give_deposits,
    OUTPOST_POPULATION,
    feed,
    make_farmable,
    rich_stockpile,
    home_colony,
    new_universe,
    take_manual_control,
)


def _outpost(
    session,
    civ,
    *,
    habitability: float,
    stockpile: dict,
    world_type: str = "barren",
    farmable: bool = False,
):
    """Plant a colony on a hand-tuned world, bypassing the colonize flow.

    ``farmable`` writes a survey that agrees with a high habitability: oceans, an
    edible ecology, temperate at one gee. Without it the world is a rock that
    feeds itself out of hydroponics, which is the right default for an outpost
    and the wrong one for a test about garden worlds.
    """
    world = session.scalars(
        select(World).where(World.colony == None).order_by(World.id)  # noqa: E711
    ).first()
    assert world is not None
    world.habitability = habitability
    world.world_type = world_type
    give_deposits(world, iron=0.015)
    if farmable:
        make_farmable(world)
    colony = Colony(
        world_id=world.id,
        civ_id=civ.id,
        name="Outpost",
        population=OUTPOST_POPULATION,
        infrastructure=1.0,
        founded_tick=0,
        stockpile=dict(stockpile),
        labor=balanced_allocation(),
    )
    # Manual: these tests drive the colony themselves, and a governor would
    # reassign labor and spend the stockpile underneath them.
    colony.management_mode = "manual"
    session.add(colony)
    session.flush()
    # Eating is not what these tests are about; keep the larder stocked so a
    # colony never fails one of them by quietly starving in the background.
    feed(colony)
    return colony


# ------------------------------------------------------------------- labor


def test_allocation_normalizes_to_the_population_that_exists():
    """Allocations are a division of real people, not a wish list."""
    allocation = normalize({EXTRACTION: 2.0, RESEARCH: 2.0})
    assert sum(allocation.values()) == pytest.approx(1.0)
    assert allocation[EXTRACTION] == pytest.approx(0.5)
    assert allocation[INDUSTRY] == 0.0
    assert set(allocation) == set(SECTORS)


def test_empty_allocation_falls_back_to_balanced():
    """An unassigned population would stop breathing. Never leave it empty."""
    assert normalize({}) == balanced_allocation()
    assert normalize({EXTRACTION: -5.0}) == balanced_allocation()


def test_labor_reallocation_shifts_output():
    """Moving people between jobs must actually move the numbers.

    Both runs start from a full warehouse, because with materials-costed
    research an empty one would make the labs idle for a reason that has nothing
    to do with who is assigned where.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=808, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        intents.set_labor(session, colony, {EXTRACTION: 1.0})
        colony.stockpile = rich_stockpile()
        colony_id = colony.id
        iron_before = colony.stockpile[IRON]

    run_ticks(engine, universe_id, 24)
    with open_session(engine) as session:
        mined_in_mining = session.get(Colony, colony_id).stockpile.get(IRON, 0.0) - iron_before
        research_in_mining = civ_by_name(session, universe_id, "Terrans").research_progress

    # Same colony, same world, everyone moved to the labs.
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = session.get(Colony, colony_id)
        intents.set_labor(session, colony, {RESEARCH: 1.0})
        colony.stockpile = rich_stockpile()
        civ.research_progress = 0.0

    run_ticks(engine, universe_id, 24)
    with open_session(engine) as session:
        mined_in_labs = session.get(Colony, colony_id).stockpile.get(IRON, 0.0) - iron_before
        research_in_labs = civ_by_name(session, universe_id, "Terrans").research_progress

    assert mined_in_mining > mined_in_labs
    assert research_in_labs > research_in_mining


def test_research_needs_materials_not_just_people():
    """Tech is bought, not banked.

    Two identical colonies, both with everyone in the labs. One has a warehouse
    and one does not, and only one of them discovers anything -- which is what
    makes an industrial base a prerequisite for a scientific one, and what makes
    a rival's laboratory world a thing you can reach by cutting its supply.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=8081, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        take_manual_control(session, civ)
        colony = home_colony(session, civ)
        intents.set_labor(session, colony, {RESEARCH: 1.0})
        colony.stockpile = {}
        colony_id = colony.id

    run_ticks(engine, universe_id, 24)
    with open_session(engine) as session:
        assert civ_by_name(session, universe_id, "Terrans").research_progress == 0.0, (
            "no materials, no research, however many people are in the labs"
        )

    with open_session(engine) as session:
        session.get(Colony, colony_id).stockpile = rich_stockpile()

    run_ticks(engine, universe_id, 24)
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        assert civ.research_progress > 0.0
        # And it was paid for out of that specific warehouse.
        assert session.get(Colony, colony_id).stockpile[ELECTRONICS] < 1e9


# ------------------------------------------------------------ life support


def test_garden_world_needs_no_life_support():
    """Habitability 1.0 means the whole population is free to work."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=809, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=1.0, stockpile={WATER: 6_250})
        colony_id = colony.id

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.stockpile[WATER] == pytest.approx(6_250), "nothing should be burned"
        assert colony.population > 0


def test_hostile_world_burns_supplies_then_starves_then_recovers():
    """The core loop that makes logistics matter.

    A dry world has no water of its own, so it cannot keep anyone alive at any
    labour allocation. It lives on its stores, dies when they run out, and comes
    back when they are restored.
    """
    # Hourly ticks: the supply clock runs in days, and at five-minute ticks
    # this test would need thousands of them to reach the interesting part.
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=810, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.0, stockpile={WATER: 5_000})
        colony_id = colony.id
        population_at_founding = colony.population

    # Phase one: alive on stores. Fifty thousand people drink 50 t of water an
    # hour, so five thousand tonnes is a hundred hours of air and a day in it is
    # comfortable.
    run_ticks(engine, universe_id, 24)
    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.stockpile[WATER] < 5_000, "hostile worlds consume supplies"
        # Growing, not dying: a dead world's habitats have headroom above a
        # landing party, so the population rises until it hits that ceiling.
        assert colony.population >= population_at_founding, "not starving yet"

    # Phase two: stores exhausted, population falling.
    run_ticks(engine, universe_id, 240)
    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.stockpile.get(WATER, 0.0) == pytest.approx(0.0, abs=1e-6)
        starving = colony.population
        assert starving < population_at_founding
        assert session.scalars(
            select(Event).where(Event.kind == "life_support_failing")
        ).all(), "a dying colony must say so in the log"

    # Phase three: a supply run arrives.
    with open_session(engine) as session:
        # Bind the colony to a name before mutating it. The session's identity
        # map holds objects weakly, so an unreferenced instance can be collected
        # before the change is flushed.
        resupplied = session.get(Colony, colony_id)
        resupplied.stockpile[WATER] = 62_500

    run_ticks(engine, universe_id, 48)
    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.population >= starving, "resupply must stop the dying"
        assert colony.stockpile[WATER] < 62_500


def test_a_dome_makes_a_hostile_world_cheaper_to_hold():
    """Domes cut what life support is needed at all."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=811, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        bare = _outpost(session, civ, habitability=0.0, stockpile={WATER: 62_500})
        bare_id = bare.id

    run_ticks(engine, universe_id, 48)
    with open_session(engine) as session:
        burned_bare = 62_500 - session.get(Colony, bare_id).stockpile[WATER]

    # Same colony, now domed.
    with open_session(engine) as session:
        colony = session.get(Colony, bare_id)
        colony.stockpile[WATER] = 62_500
        session.add(
            Building(colony_id=colony.id, kind="dome", work_remaining=0.0, completed_tick=0)
        )

    run_ticks(engine, universe_id, 48)
    with open_session(engine) as session:
        burned_domed = 62_500 - session.get(Colony, bare_id).stockpile[WATER]

    assert burned_domed < burned_bare


def test_hydroponics_recycles_supplies():
    """Where a dome cuts the need, hydroponics cuts what meeting it costs."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=812, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.0, stockpile={WATER: 62_500})
        colony_id = colony.id

    run_ticks(engine, universe_id, 48)
    with open_session(engine) as session:
        burned_plain = 62_500 - session.get(Colony, colony_id).stockpile[WATER]

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        colony.stockpile[WATER] = 62_500
        session.add(
            Building(
                colony_id=colony.id, kind="hydroponics", work_remaining=0.0, completed_tick=0
            )
        )

    run_ticks(engine, universe_id, 48)
    with open_session(engine) as session:
        burned_recycled = 62_500 - session.get(Colony, colony_id).stockpile[WATER]

    assert burned_recycled < burned_plain


# --------------------------------------------------------------- buildings


def test_fleet_construction_requires_a_shipyard():
    """A frontier outpost is not automatically a naval base."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=813, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.8, stockpile=rich_stockpile())
        intents.build_fleet(session, civ, colony.id, 1.0)
        colony_id = colony.id

    run_ticks(engine, universe_id, 2)
    with open_session(engine) as session:
        intent = session.scalar(select(intents.Intent).where(intents.Intent.kind == "build_fleet"))
        assert intent.status == IntentStatus.FAILED.value
        assert "shipyard" in intent.result

    # With a yard, the same order goes through -- ordered at the capital, which
    # is where hulls actually get built. A hull is hundreds of thousands of
    # tonnes and days of industry, so a mining outpost has the slipway and no
    # way to use it, which is the point of gating on the yard *and* the price.
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        capital = home_colony(session, civ)
        take_manual_control(session, civ)
        capital.stockpile = rich_stockpile(1e12)
        intents.set_labor(session, capital, {INDUSTRY: 1.0})
        intents.build_fleet(session, civ, capital.id, 1.0, name="Second Wave")
        fleets_before = len(session.scalars(select(Fleet).where(Fleet.civ_id == civ.id)).all())

    run_ticks(engine, universe_id, 400)
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        fleets = session.scalars(select(Fleet).where(Fleet.civ_id == civ.id)).all()
        assert len(fleets) == fleets_before + 1


def test_buildings_need_industry_labor_to_finish():
    """Construction is paid in industry-work, not wall-clock time.

    A colony with nobody in industry sits on a foundation forever, which is what
    makes the labor allocation a decision rather than a display.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=814, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        colony.stockpile = rich_stockpile()
        intents.set_labor(session, colony, {EXTRACTION: 1.0})  # nobody building
        # A collector, because the capital does not already have one: this test
        # is about a *fresh* level 1 finishing or not, and deepening one of the
        # capital's existing three-hundred-level industries is a season's work
        # whatever the labour allocation says.
        intents.build_structure(session, civ, colony.id, "collector")
        colony_id = colony.id

    run_ticks(engine, universe_id, 100)
    with open_session(engine) as session:
        lab = session.scalar(select(Building).where(Building.kind == "collector"))
        assert lab is not None, "foundations should still be laid"
        assert not lab.is_complete, "nobody is working on it"

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        intents.set_labor(session, colony, {INDUSTRY: 1.0})

    run_ticks(engine, universe_id, 100)
    with open_session(engine) as session:
        lab = session.scalar(select(Building).where(Building.kind == "collector"))
        assert lab.is_complete
        assert lab.completed_tick is not None


def test_industry_is_limited_by_people_and_ground():
    """A world's ceiling is physical, not a slot count.

    An industry needs staff to run it and land to stand on. A colony that has
    run out of either cannot develop further until it grows -- which is a
    statement about the place rather than an arbitrary allowance, and it means a
    cramped moon and a continent are genuinely different propositions.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=815, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        colony.stockpile = rich_stockpile()
        # Strip the world down to a body that can site almost nothing, and the
        # population down to a village that could not staff it anyway.
        colony.world.land_area_km2 = 1.0
        colony.population = 100.0
        intents.build_structure(session, civ, colony.id, "laboratory")

    run_ticks(engine, universe_id, 2)

    with open_session(engine) as session:
        intent = session.scalar(
            select(intents.Intent).where(intents.Intent.kind == "build_structure")
        )
        assert intent.status == IntentStatus.FAILED.value
        assert "staff or site" in intent.result


def test_the_binding_limit_is_whichever_runs_out_first():
    """A crowded moon and an empty continent fail in opposite directions."""
    # Plenty of people, almost no ground: the moon case.
    assert max_total_levels(population=1e10, land_area_km2=90_000.0) == 1
    assert binding_limit(1e10, 90_000.0) == "ground"

    # Plenty of ground, almost nobody: the frontier case.
    assert max_total_levels(population=250_000.0, land_area_km2=1e9) == 1
    assert binding_limit(250_000.0, 1e9) == "people"

    # Both rise as the colony develops, and a landing party can always build
    # *something* or it could never bootstrap.
    assert max_total_levels(population=0.0, land_area_km2=0.0) >= 1
    assert max_total_levels(1e10, 1e9) > max_total_levels(1e8, 1e9)


def test_ordering_an_industry_again_deepens_it():
    """Buildings are industries with levels, not things you have or do not.

    A second mine on a world that already has one is not a second mine -- it is
    the mining sector getting bigger. Each level costs proportionally more and
    returns proportionally less, so development has real diminishing returns and
    a civilization is eventually better off founding a new colony than deepening
    an old one.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=816, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        take_manual_control(session, civ)
        intents.set_labor(session, colony, {INDUSTRY: 1.0})
        colony.stockpile = rich_stockpile(1e12)
        colony_id = colony.id
        # An industry the capital has not started, so both levels are cheap
        # enough to watch inside a test. The semantics are what is under test,
        # not the calendar -- see ``tests/test_prices.py`` for how long a deep
        # level actually takes, which is months.
        intents.build_structure(session, civ, colony.id, "collector")

    run_ticks(engine, universe_id, 200)
    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        built = [b for b in colony.buildings if b.kind == "collector"]
        assert len(built) == 1 and built[0].level == 1 and built[0].is_complete
        civ = civ_by_name(session, universe_id, "Terrans")
        intents.build_structure(session, civ, colony_id, "collector")

    run_ticks(engine, universe_id, 200)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        built = [b for b in colony.buildings if b.kind == "collector"]
        assert len(built) == 1, "an industry deepens rather than duplicating"
        assert built[0].level == 2
        assert built[0].is_complete, "and the level should have finished"


def test_unknown_building_kind_fails_with_a_helpful_message():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=817, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        intents.build_structure(session, civ, home_colony(session, civ).id, "orbital_casino")

    run_ticks(engine, universe_id, 2)

    with open_session(engine) as session:
        intent = session.scalar(
            select(intents.Intent).where(intents.Intent.kind == "build_structure")
        )
        assert intent.status == IntentStatus.FAILED.value
        assert "shipyard" in intent.result, "the error should list what is valid"


def test_building_catalogue_is_coherent():
    """Guards the hand-authored table against obvious authoring slips."""
    from galaxysim.colony.buildings import BUILDING_TYPES

    kinds = [b.kind for b in BUILDING_TYPES]
    assert len(kinds) == len(set(kinds)), "duplicate building kind"

    for spec in BUILDING_TYPES:
        assert spec.work > 0, f"{spec.kind} would finish instantly"
        assert spec.cost, f"{spec.kind} is free"
        assert all(amount > 0 for amount in spec.cost.values())
        assert 0.0 <= spec.life_support_recycling < 1.0
        assert building_type(spec.kind) is spec

    for sector in (EXTRACTION, INDUSTRY, RESEARCH, LIFE_SUPPORT):
        assert any(
            sector in spec.sector_bonus or spec.resource_bonus for spec in BUILDING_TYPES
        ), f"nothing improves {sector}"


# --------------------------------------------------------------- expedition


class _StubWorld:
    """Enough of a World for :func:`assess`.

    Which now means the *promoted columns* rather than the survey document:
    ``assess`` used to decode a whole planet to learn whether it had surface
    water and how fast ice came out of the ground, and both have been columns
    since they were promoted. ``give_deposits`` fills ``extraction`` in through
    ``refresh_promoted``, the same way it does for a real world.
    """

    def __init__(self, habitability: float, survey: dict) -> None:
        self.habitability = habitability
        self.survey = survey
        self.surface_water = False
        self.extraction: dict[str, float] = {}


def _stub_world(habitability: float, **yields: float) -> _StubWorld:
    world = _StubWorld(habitability, {})
    give_deposits(world, **yields)
    return world


def test_loadout_cost_scales_with_what_you_send():
    """No flat fee: the price is the manifest."""
    light = Loadout(colonists=20_000.0, equipment=2.0, stores=10_000.0)
    heavy = Loadout(colonists=60_000.0, equipment=8.0, stores=200_000.0)

    light_cost, heavy_cost = light.cost(), heavy.cost()
    assert all(heavy_cost[r] > light_cost[r] for r in light_cost)
    # And the manifest is priced in what it actually is: an expedition carrying
    # two hundred units of life-support stores is mostly water and food by
    # weight, not steel.
    assert heavy_cost[WATER] > heavy_cost[STEEL]
    assert set(heavy_cost) <= set(MATERIALS), "every line must be a real material"


def test_equipment_buys_a_more_capable_colony():
    """Loadout decides what the colony wakes up with, not just its price."""
    bare = Loadout(colonists=50_000.0, equipment=0.0, stores=0.0)
    outfitted = Loadout(colonists=50_000.0, equipment=10.0, stores=0.0)

    assert outfitted.starting_infrastructure() > bare.starting_infrastructure()
    assert bare.starting_infrastructure() > 0, "even a bare landing can do something"


def test_construction_gets_the_industry_refining_could_not_spend():
    """The pipeline's step 6 says so; for a long time the code did not.

    ``production.py`` has always opened by describing construction as spending
    *whatever industry-work refining left*, and ``construction_output`` returned
    a flat ``industry_output x (1 - refining_share)`` instead. Those differ
    because refining is limited by **ore in the warehouse**, not by labour: over
    a real simulated day of a developed capital's chains it spent 21.6 M of the
    368.1 M it was offered and the rest was discarded, so a capital lost 47% of
    its total industry every tick to a reservation it could not use.

    Two colonies, because one cannot tell a change from a constant: a warehouse
    with nothing to refine must beat the flat fraction, and one whose chains can
    absorb their whole share must land exactly on it.
    """
    from galaxysim.engine.context import TickContext
    from galaxysim.engine.rates import CADENCE_HOURLY, DEFAULT_RATES
    from galaxysim.engine.resolvers.production import (
        _refine,
        construction_output,
        industry_output,
    )
    from galaxysim.model.entities import Universe

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=822, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        ctx = TickContext.build(session, universe, CADENCE_HOURLY, DEFAULT_RATES)

        total = industry_output(ctx, colony)
        flat = total * (1.0 - DEFAULT_RATES.refining_share_of_industry)
        assert flat > 0, "a reference colony that builds nothing measures nothing"

        # Nothing in the warehouse to process, so every tonne of the refining
        # reservation is unspendable and the whole of it is owed to the yard.
        starved: dict[str, float] = {}
        _refine(ctx, colony, starved)
        assert construction_output(ctx, colony) == pytest.approx(total), (
            "a colony with no ore refines nothing, so all of its industry should "
            "reach construction; if this reads the flat fraction the reservation "
            "is still evaporating"
        )

        # And the other end: chains that can absorb the entire share leave the
        # yard exactly what it always had.
        ctx.invalidate(("refining_spent", colony.id))
        _refine(ctx, colony, rich_stockpile())
        assert construction_output(ctx, colony) == pytest.approx(flat, rel=1e-9), (
            "a colony whose chains spend their whole share must leave "
            "construction the fraction every price in the game was calibrated "
            "against"
        )


def test_the_construction_readout_is_the_work_the_yard_actually_did():
    """One number, not two -- the guard this file has a scar for.

    ``construction_output`` is both an engine input and a readout: the terraform
    planner quotes it, the empire view prints it, and the resolver above spends
    it. Letting the two drift is how a terraforming campaign came to be quoted at
    eleven times its real length. Now that the figure depends on what refining
    happened to spend this tick, there is a fresh way for them to part company,
    so this asserts they cannot: a building under construction must lose exactly
    the work the readout claims was available.
    """
    from galaxysim.engine.context import TickContext
    from galaxysim.engine.rates import CADENCE_HOURLY, DEFAULT_RATES
    from galaxysim.engine.resolvers.production import construction_output
    from galaxysim.model.entities import Universe

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=823, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = home_colony(session, civ)
        # Water and food only: no ore, so the chains have nothing to process and
        # the reservation goes almost entirely unspent. That gap is the whole
        # point -- with a full warehouse refining absorbs its share, the stored
        # figure and the reservation agree, and this test cannot tell them apart.
        # It could not, in its first draft, and only the mutation said so.
        colony.stockpile = {WATER: 1e9, FOOD: 1e9}
        # Exactly one thing under construction, so the whole of the tick's
        # construction work goes into it and the arithmetic is unambiguous.
        for building in colony.buildings:
            building.work_remaining = 0.0
            building.completed_tick = 0
        session.add(
            Building(
                colony_id=colony.id,
                kind="mine",
                level=1,
                work_remaining=1e12,  # far more than one tick can finish
                started_tick=0,
            )
        )
        colony_id = colony.id

    # Capture what the resolver used while the tick was running. Comparing a
    # *pre*-tick quote against the next tick's work would measure the colony
    # changing in between -- it mines ore, its chains find more to do -- rather
    # than the two figures agreeing, which is what this is about.
    import galaxysim.engine.resolvers.production as production

    inside: list[float] = []
    real = production.construction_output

    def spy(ctx, colony):
        value = real(ctx, colony)
        if colony.id == colony_id:
            inside.append(value)
        return value

    production.construction_output = spy
    try:
        run_ticks(engine, universe_id, 1)
    finally:
        production.construction_output = real

    assert inside, "the colony built nothing, so this measured nothing"
    spent_in_tick = inside[-1]

    # Now the question a player's readout asks: same colony, same state, but
    # from outside a tick, where the memo is gone and only the stored column is
    # left to go on.
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        colony = session.get(Colony, colony_id)
        ctx = TickContext.build(session, universe, CADENCE_HOURLY, DEFAULT_RATES)
        quoted = construction_output(ctx, colony)

    assert quoted == pytest.approx(spent_in_tick, rel=0.02), (
        f"the yard had {spent_in_tick:,.0f} of construction work and a player "
        f"asking afterwards is told {quoted:,.0f}; the number shown and the "
        "number spent have come apart"
    )


def test_a_manifest_is_cut_down_to_what_the_warehouse_holds():
    """The unit of the fix: what a short expedition sails with.

    Equipment goes first because it is productivity and the only line needing
    electronics; stores are protected behind it because they are survival.
    """
    wanted = Loadout(colonists=50_000.0, equipment=4.0, stores=40_000.0)
    full = wanted.cost()

    # Everything except the electronics a unit of equipment needs.
    without_electronics = {key: value for key, value in full.items() if key != ELECTRONICS}
    reduced = wanted.largest_within(without_electronics)
    assert reduced is not None, "the colonists were fully supplied; it should sail"
    assert reduced.equipment == 0.0, "equipment is the line that needs electronics"
    assert reduced.colonists == wanted.colonists, "people are never cut"
    assert reduced.stores == wanted.stores, "stores are protected behind equipment"

    # A warehouse holding the lot changes nothing.
    assert wanted.largest_within(full) == wanted

    # And nothing at all is the one case that still refuses: an expedition that
    # cannot feed its own colonists is not a poorer colony, it is a funeral.
    assert wanted.largest_within({}) is None


def test_a_crust_with_no_copper_does_not_end_a_civilization():
    """The regression for the whole phase.

    Electronics needs copper *and* rare earths, a unit of colony equipment needs
    three thousand tonnes of electronics, and a homeworld drawn without either
    element therefore could not outfit an expedition at all. Measured over thirty
    days of eight opponents, one sat on a habitability-1.0 world with eleven
    billion people and two colonies, cycling order -> fortnight -> abandon ->
    order for the whole run. Its geology had ended it before it played.

    ``expedition.py`` says this game never refuses a legal order. Now it does not.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=820, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        # Rich in everything the expedition needs except the one chain this
        # world's crust cannot feed.
        home.stockpile = {
            key: 0.0 if key in NO_ELECTRONICS_CHAIN else value
            for key, value in rich_stockpile().items()
        }
        give_deposits(home.world, iron=0.4, silicon=0.6)  # no copper, no rare earths

        rock = next(
            w
            for w in sorted(home.world.system.worlds, key=lambda w: w.id)
            if w.colony is None
        )
        give_deposits(rock, iron=0.02)
        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        intents.colonize(
            session,
            civ,
            fleet.id,
            rock.id,
            loadout=Loadout(colonists=50_000.0, equipment=4.0, stores=40_000.0),
            name="Copperless",
        )

    # Past the fortnight the outfitting loop waits before giving up.
    run_ticks(engine, universe_id, 24 * 15)

    with open_session(engine) as session:
        colony = session.scalar(select(Colony).where(Colony.name == "Copperless"))
        assert colony is not None, (
            "a civilization whose crust cannot make electronics must still be "
            "able to settle; it is the difference between a hard start and no game"
        )
        assert colony.population == pytest.approx(50_000.0, rel=0.1)
        assert session.scalars(
            select(Event).where(Event.kind == "expedition_sailed_short")
        ).all(), "and the player is told the manifest was cut"


def test_sailing_short_lands_a_worse_colony_rather_than_a_free_one():
    """The counterpart, and the thing that stops this being a loophole.

    If a short expedition landed the same colony, equipment would be optional
    for everybody and the whole electronics chain would stop mattering.
    """
    def _settle(with_electronics: bool) -> float:
        engine = create_engine_for("sqlite://")
        universe_id = new_universe(engine, seed=821, civs=("Terrans",), seconds_per_tick=3600)
        with open_session(engine) as session:
            civ = civ_by_name(session, universe_id, "Terrans")
            home = home_colony(session, civ)
            home.stockpile = rich_stockpile()
            if not with_electronics:
                home.stockpile = {
                    key: 0.0 if key in NO_ELECTRONICS_CHAIN else value
                    for key, value in home.stockpile.items()
                }
                give_deposits(home.world, iron=0.4, silicon=0.6)
            rock = next(
                w
                for w in sorted(home.world.system.worlds, key=lambda w: w.id)
                if w.colony is None
            )
            give_deposits(rock, iron=0.02)
            fleet = session.scalar(
                select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id)
            )
            intents.colonize(
                session,
                civ,
                fleet.id,
                rock.id,
                loadout=Loadout(colonists=50_000.0, equipment=4.0, stores=40_000.0),
                name="Landing",
            )
        run_ticks(engine, universe_id, 24 * 15)
        with open_session(engine) as session:
            colony = session.scalar(select(Colony).where(Colony.name == "Landing"))
            assert colony is not None
            return colony.infrastructure

    supplied = _settle(with_electronics=True)
    short = _settle(with_electronics=False)

    assert short < supplied, (
        f"a colony landed by a short expedition came up with {short} "
        f"infrastructure against {supplied} for a supplied one; if these match, "
        "equipment has quietly become optional and geology stopped mattering"
    )
    assert short > 0, "even a bare landing can do something"


def test_hostility_is_priced_through_what_survival_requires():
    """A hard world costs more because it needs more, not via a surcharge.

    This is the design decision the whole pricing model rests on: settle a
    garden world and a bare rock with the *same* manifest and you pay exactly
    the same. What differs is that on the rock the manifest is not enough.
    """
    from galaxysim.engine.rates import DEFAULT_RATES

    loadout = Loadout(colonists=50_000.0, equipment=4.0, stores=40_000.0)
    garden = assess(loadout, _stub_world(1.0, water_ice=0.01), DEFAULT_RATES)
    rock = assess(loadout, _stub_world(0.0, iron=0.02), DEFAULT_RATES)

    assert garden.cost == rock.cost, "identical manifests cost the same anywhere"
    assert garden.survival_hours is None and garden.self_sufficient
    assert rock.survival_hours is not None and not rock.self_sufficient
    assert rock.burn_per_hour > 0
    assert "supply route" in rock.summary()


def test_a_world_with_ice_in_the_ground_stands_alone():
    """Water is the difference between a place and a permanent liability.

    Not "volatiles" as a category -- ice, specifically, in this crust, refined
    here. A colony with none of it breathes at the end of a supply line no
    matter how rich it is in everything else.
    """
    from galaxysim.engine.rates import DEFAULT_RATES

    icy = assess(
        Loadout(colonists=50_000.0, equipment=6.0, stores=50_000.0),
        _stub_world(0.1, water_ice=0.05),
        DEFAULT_RATES,
    )
    dry = assess(
        Loadout(colonists=50_000.0, equipment=6.0, stores=50_000.0),
        _stub_world(0.1, iron=0.05),
        DEFAULT_RATES,
    )
    assert not dry.self_sufficient, "no ice, no water, no independence"
    verdict = icy
    assert verdict.self_sufficient
    assert "Self-sufficient" in verdict.summary()


def test_uninhabitable_worlds_can_be_settled_and_live_on_their_stores():
    """The payoff: the richest worlds are settleable, and fragile.

    Barren worlds and gas giants carry the best yields in the table and cannot
    keep anyone alive on their own. Settling one is legal, and it starts a
    countdown.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=818, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = rich_stockpile()

        # A dead rock in the home system, so the fleet is already there.
        rock = next(
            w
            for w in sorted(home.world.system.worlds, key=lambda w: w.id)
            if w.colony is None
        )
        rock.habitability = 0.0
        rock.world_type = "barren"
        give_deposits(rock, iron=0.02, water_ice=0.0)
        rock_id = rock.id

        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        intents.colonize(
            session,
            civ,
            fleet.id,
            rock_id,
            loadout=Loadout(colonists=50_000.0, equipment=4.0, stores=100_000.0),
            name="Deep Rock",
        )

    run_ticks(engine, universe_id, 12)

    with open_session(engine) as session:
        colony = session.scalar(select(Colony).where(Colony.name == "Deep Rock"))
        assert colony is not None, "an uninhabitable world must be settleable"
        assert colony.population == pytest.approx(50_000.0, rel=0.1), (
            "the colonists became the population"
        )
        assert colony.stockpile[WATER] > 0, "stores land with them"
        assert colony.infrastructure > 0.5, "equipment becomes infrastructure"
        assert session.scalars(
            select(Event).where(Event.kind == "colony_founded")
        ).all()

    # It mines well, and it is burning through its air the whole time.
    run_ticks(engine, universe_id, 72)
    with open_session(engine) as session:
        colony = session.scalar(select(Colony).where(Colony.name == "Deep Rock"))
        assert colony.stockpile[IRON] > 0, "a rich world, while it lives"
        assert colony.stockpile[WATER] < 70_000.0, "and a clock running down"


def test_an_expedition_with_no_stores_dies_on_a_dead_world():
    """A bad loadout is allowed to fail. The game warns; it does not refuse."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=819, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        home = home_colony(session, civ)
        home.stockpile = rich_stockpile()

        rock = next(
            w for w in sorted(home.world.system.worlds, key=lambda w: w.id) if w.colony is None
        )
        rock.habitability = 0.0
        give_deposits(rock, iron=0.02, water_ice=0.0)

        fleet = session.scalar(select(Fleet).where(Fleet.civ_id == civ.id).order_by(Fleet.id))
        intents.colonize(
            session,
            civ,
            fleet.id,
            rock.id,
            loadout=Loadout(colonists=20_000.0, equipment=1.0, stores=0.0),
            name="Doomed",
        )

    run_ticks(engine, universe_id, 400)

    with open_session(engine) as session:
        colony = session.scalar(select(Colony).where(Colony.name == "Doomed"))
        assert colony is not None
        assert colony.population < 0.5, "no air, no colony"
        assert session.scalars(
            select(Event).where(Event.kind == "life_support_failing")
        ).all()


# ---------------------------------------------------------------- governors


def test_new_colonies_are_governed_by_default():
    """Depth is opt-in. A large empire must not require managing every world."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=820, civs=("Terrans",))

    with open_session(engine) as session:
        colony = home_colony(session, civ_by_name(session, universe_id, "Terrans"))
        assert colony.is_governed
        assert colony.governor_policy == "balanced"


def test_setting_labor_by_hand_takes_the_colony_off_the_governor():
    """Otherwise the governor would overwrite the order on the next tick."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=821, civs=("Terrans",))

    with open_session(engine) as session:
        colony = home_colony(session, civ_by_name(session, universe_id, "Terrans"))
        intents.set_labor(session, colony, {RESEARCH: 1.0})
        colony_id = colony.id
        assert not colony.is_governed

    run_ticks(engine, universe_id, 10)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.labor[RESEARCH] == pytest.approx(1.0), "the order must stick"


def test_a_governor_keeps_a_hostile_colony_breathing():
    """A governed colony on a bad world reserves labor for life support.

    And it does so regardless of the policy it was handed: told to chase
    research on a rock that cannot breathe, it stays alive first.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=822, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.0, stockpile={WATER: 625_000})
        intents.set_management(session, colony, governed=True, policy="research")
        colony_id = colony.id

    run_ticks(engine, universe_id, 48)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.labor[LIFE_SUPPORT] > 0.1, "a governor must fund life support"
        assert colony.population > 0


def test_a_governed_garden_world_spends_nothing_on_life_support():
    """The converse: no bill, no reserve, everyone works."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=823, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=1.0, stockpile={}, farmable=True)
        intents.set_management(session, colony, governed=True, policy="extraction")
        colony_id = colony.id

    run_ticks(engine, universe_id, 5)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert colony.labor[LIFE_SUPPORT] == pytest.approx(0.0)
        assert colony.labor[EXTRACTION] > 0.5


def test_a_governor_gets_no_hidden_bonus():
    """Delegation is convenience, never advantage.

    Two identical colonies, one governed and one set by hand to the exact
    allocation the governor chose, must produce the same. A player who
    micromanages well should never be losing to the autopilot.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=824, civs=("Terrans",))

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        governed = _outpost(session, civ, habitability=0.8, stockpile={})
        intents.set_management(session, governed, governed=True, policy="extraction")
        governed_id = governed.id

    # One tick to let the governor choose, then copy its allocation onto a twin.
    run_ticks(engine, universe_id, 1)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        governed = session.get(Colony, governed_id)
        chosen = dict(governed.labor)
        # It mined during the tick that let the governor choose; start both from
        # nothing so the comparison is of output, not of a head start.
        governed.stockpile = {}

        twin = _outpost(session, civ, habitability=0.8, stockpile={})
        twin.name = "Twin"
        # The same planet, not merely a similar one -- otherwise this measures
        # two worlds' geology rather than two ways of running a colony.
        clone_world(governed.world, twin.world)
        twin.population = governed.population
        twin.infrastructure = governed.infrastructure
        intents.set_labor(session, twin, chosen)
        twin_id = twin.id

    run_ticks(engine, universe_id, 24)

    with open_session(engine) as session:
        a = session.get(Colony, governed_id).stockpile.get(IRON, 0.0)
        b = session.get(Colony, twin_id).stockpile.get(IRON, 0.0)
        assert a == pytest.approx(b, rel=0.02)


def test_a_governor_develops_a_colony_over_time():
    """Left alone, a governed colony should build something useful."""
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=825, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        # A world that can supply its own chains end to end: ore for steel and
        # construction materials, ice for the water its people breathe.
        #
        # It opens with one delivery's worth of goods rather than an empty
        # warehouse, because an industry is now tens of thousands of tonnes and
        # fifty thousand people mine a few tonnes an hour -- so a colony left
        # entirely to itself spends a year banking its first mine. That is the
        # right answer and it is what makes a supply line matter, but it is not
        # what this test is about, which is whether a governor left alone
        # actually develops the place.
        colony = _outpost(
            session, civ, habitability=0.9, stockpile=rich_stockpile(2.0e5), farmable=True
        )
        give_deposits(
            colony.world, iron=0.03, silicon=0.03, calcium=0.02, carbon=0.01, water_ice=0.02
        )
        intents.set_management(session, colony, governed=True, policy="extraction")
        colony_id = colony.id
        buildings_before = len(colony.buildings)

    run_ticks(engine, universe_id, 500)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert len(colony.buildings) > buildings_before
        assert any(b.is_complete for b in colony.buildings), "and finish at least one"


def test_a_finished_building_finishes_the_order_that_asked_for_it():
    """The order that nothing ever closed.

    ``BUILD_STRUCTURE`` appeared exactly once in the production resolver: the
    place that charged for it, laid the foundations and set it ``IN_PROGRESS``.
    Nothing completed it, ever. The building went up, ``construction_completed``
    was logged, and the order stayed open for the rest of the game -- which a
    player saw as a build that had been finished for weeks still sitting in
    ``galaxysim orders``.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=828, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.9, stockpile=rich_stockpile(2.0e5))
        intents.build_structure(session, civ, colony.id, "mine")
        colony_id = colony.id

    run_ticks(engine, universe_id, 400)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        mine = next(b for b in colony.buildings if b.kind == "mine")
        assert mine.is_complete, "the fixture needs the building to actually finish"

        order = session.scalar(
            select(Intent).where(
                Intent.civ_id == colony.civ_id, Intent.kind == IntentKind.BUILD_STRUCTURE.value
            )
        )
        assert order.status == IntentStatus.COMPLETED.value, (
            f"the mine is built and its order is still {order.status!r}"
        )
        assert order.resolved_tick is not None


def test_a_governor_builds_a_second_thing_after_the_first_one_lands():
    """The consequence, and the reason the open order mattered so much.

    ``governor._maybe_build`` returns early when the colony is already building
    something, and it learns that from the *orders*, not the buildings. An order
    that never closed meant every governed colony in the game built exactly one
    structure and then stopped, permanently -- eighty-five colonies produced
    sixteen buildings in a sixty-day soak, and every homeworld sat at half power
    for the whole run because it could never build another reactor.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=829, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        # The homeworld, not an outpost. Fifty thousand colonists can staff
        # exactly one industry level, so an outpost cannot build a second thing
        # however long it runs -- it would pass this test for the wrong reason,
        # or fail it for one. A capital has the people and the ground.
        colony = home_colony(session, civ)
        colony.stockpile = rich_stockpile(1.0e9)
        give_deposits(
            colony.world, iron=0.03, silicon=0.03, calcium=0.02, carbon=0.01, water_ice=0.02
        )
        assert max_total_levels(colony.population, colony.world.land_area_km2) > levels_in_use(
            colony.buildings
        ) + 2, "the fixture needs room to build more than one thing"
        intents.set_management(session, colony, governed=True, policy="extraction")
        colony_id = colony.id
        # Levels, not buildings. A developed capital already has one of most
        # things, so what it does with a second order is *deepen* an industry --
        # the building count is unchanged and the levels are what move.
        before = levels_in_use(colony.buildings)

    run_ticks(engine, universe_id, 900)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        orders = session.scalars(
            select(Intent).where(
                Intent.civ_id == colony.civ_id, Intent.kind == IntentKind.BUILD_STRUCTURE.value
            )
        ).all()
        assert len(orders) > 1, (
            f"the governor queued {len(orders)} structure(s) and then stopped for good"
        )
        assert sum(o.status == IntentStatus.COMPLETED.value for o in orders) > 1, (
            "and more than one of them has to have finished"
        )
        assert levels_in_use(colony.buildings) > before + 1, (
            "so the colony is measurably deeper than it was"
        )


def test_a_captured_colony_does_not_leave_its_old_owner_an_open_order():
    """An order that can never finish is the same bug wearing a different hat.

    Worlds change hands now, and a capture reassigns the colony under whatever
    was rising on it. Left open, that order would jam the *new* owner's governor
    exactly the way the missing completion jammed everybody's -- a colony that
    can never build again, for a reason nothing in the game displays.
    """
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(
        engine, seed=830, civs=("Terrans", "Rivals"), seconds_per_tick=3600
    )

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        rival = civ_by_name(session, universe_id, "Rivals")
        colony = _outpost(session, civ, habitability=0.9, stockpile=rich_stockpile(2.0e5))
        intents.build_structure(session, civ, colony.id, "mine")
        colony_id, rival_id = colony.id, rival.id

    run_ticks(engine, universe_id, 1)  # the order starts and the foundations go in

    with open_session(engine) as session:
        order = session.scalar(
            select(Intent).where(Intent.kind == IntentKind.BUILD_STRUCTURE.value)
        )
        assert order.status == IntentStatus.IN_PROGRESS.value, "the fixture needs it under way"
        session.get(Colony, colony_id).civ_id = rival_id  # the world is taken

    run_ticks(engine, universe_id, 2)

    with open_session(engine) as session:
        order = session.scalar(
            select(Intent).where(Intent.kind == IntentKind.BUILD_STRUCTURE.value)
        )
        assert order.status == IntentStatus.FAILED.value, (
            f"the colony belongs to somebody else and the order is still {order.status!r}"
        )
        assert "no longer belongs" in (order.result or "")


def test_a_governor_builds_what_it_can_afford_rather_than_nothing():
    """The rule that left the best world in the game with six factories.

    A governor used to stop walking its build order at the first entry it could
    not pay for, on the reasoning that saving up for the thing its policy wants
    most beats always building the cheapest shed. That holds while a colony is
    *accumulating*, and nothing checked whether it was. Measured at day 60: a
    terraformed garden of 24.8 billion people with room for 7,122 industry levels
    had six, waiting on a factory priced at 1.62 million tonnes of steel while
    holding 777 thousand — and a mine and a refinery it could have paid for that
    afternoon went unbuilt for two months.
    """
    from galaxysim.engine.resolvers.governor import _BUILD_ORDER, INDUSTRY_POLICY

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=831, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.9, stockpile={})
        # Enough for a mine, not for the factory that heads the industry order
        # and not for the refinery behind it.
        colony.stockpile = {STEEL: 35_000.0, CONSTRUCTION: 28_000.0}
        assert not can_afford(
            colony.stockpile, cost_of_level(building_type("factory").cost, 1)
        ), "the fixture needs the head of the order to be out of reach"
        assert can_afford(
            colony.stockpile, cost_of_level(building_type("mine").cost, 1)
        ), "and something further down it to be within reach"
        assert _BUILD_ORDER[INDUSTRY_POLICY].index("factory") < _BUILD_ORDER[
            INDUSTRY_POLICY
        ].index("mine"), "the fixture assumes the factory is ahead of the mine"
        intents.set_management(session, colony, governed=True, policy=INDUSTRY_POLICY)
        colony_id = colony.id

    run_ticks(engine, universe_id, 3)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert [b.kind for b in colony.buildings] == ["mine"], (
            "it could not afford a factory and built nothing at all, rather than "
            f"the mine it could pay for; built {[b.kind for b in colony.buildings]}"
        )


def test_an_industry_world_can_refine_its_own_ore():
    """The one policy that could not make the material everything it builds needs.

    Factories, shipyards and spaceports are all priced in steel; steel is refined
    from iron; a refinery is what puts a colony's industry behind that
    conversion. The industry build order had no refinery in it, so the policy
    dedicated to being an empire's workshop was the only one guaranteed to run
    out of the one material every entry on its own list is priced in — measured,
    a world sitting on 10.9 million tonnes of iron and 777 thousand of steel.
    """
    from galaxysim.engine.resolvers.governor import INDUSTRY_POLICY

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=832, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.9, stockpile={})
        # Enough for a refinery and for nothing else on the list: the factory
        # ahead of it wants construction and electronics, the mine behind it
        # wants construction.
        colony.stockpile = {STEEL: 35_000.0, CERAMICS: 20_000.0}
        assert can_afford(
            colony.stockpile, cost_of_level(building_type("refinery").cost, 1)
        )
        intents.set_management(session, colony, governed=True, policy=INDUSTRY_POLICY)
        colony_id = colony.id

    run_ticks(engine, universe_id, 3)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert [b.kind for b in colony.buildings] == ["refinery"], (
            "an industry world should be able to build the thing that turns its "
            f"ore into steel; built {[b.kind for b in colony.buildings]}"
        )


def test_a_governor_makes_what_the_colony_is_short_of():
    """The rule that stopped an empire dying of thirst beside a full warehouse.

    Nothing set ``Colony.refining`` for the whole life of the project, so every
    colony ran the deliberately mediocre even plan and never once noticed a
    shortage. An AI empire reached seven hundred million tonnes of alloys while
    making no fuel at all, flew on its homeworld's opening bank for twenty-six
    days, and lost a third of its navy in the two days after it emptied.
    """
    from galaxysim.engine.resolvers.governor import _refining_for
    from galaxysim.materials import ALLOYS, CARBON, FUEL, WATER_ICE

    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=827, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.9, stockpile={}, farmable=True)
        # Drowning in alloys, out of fuel, and holding both fuel inputs.
        colony.stockpile = {
            ALLOYS: 700_000_000.0,
            WATER_ICE: 5_000_000.0,
            CARBON: 5_000_000.0,
            FUEL: 0.0,
        }
        weights = _refining_for(colony)

        assert "fuel_synthesis" in weights, "it can run the chain and it needs the output"
        assert weights["fuel_synthesis"] > weights.get("light_alloys", 0.0), (
            "a colony with no fuel and 700 Mt of alloys should be making fuel"
        )

        # And it swings back: stock the fuel and the emphasis moves on.
        colony.stockpile[FUEL] = 700_000_000.0
        colony.stockpile[ALLOYS] = 0.0
        after = _refining_for(colony)
        assert after["fuel_synthesis"] < weights["fuel_synthesis"]


def test_a_governor_respects_slot_limits():
    engine = create_engine_for("sqlite://")
    universe_id = new_universe(engine, seed=826, civs=("Terrans",), seconds_per_tick=3600)

    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        colony = _outpost(session, civ, habitability=0.9, stockpile={}, farmable=True)
        # A cramped body: enough ground for two levels and no more, however
        # rich it is and however long the governor is left alone.
        colony.world.land_area_km2 = 2.0 * KM2_PER_LEVEL
        give_deposits(colony.world, iron=0.03, silicon=0.03, calcium=0.02, carbon=0.01)
        intents.set_management(session, colony, governed=True, policy="extraction")
        colony_id = colony.id

    run_ticks(engine, universe_id, 800)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        assert levels_in_use(colony.buildings) <= max_total_levels(
            colony.population, colony.world.land_area_km2
        )
