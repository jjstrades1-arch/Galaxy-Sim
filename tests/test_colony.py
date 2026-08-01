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
from galaxysim.materials import ELECTRONICS, IRON, MATERIALS, STEEL, WATER
from galaxysim.engine import intents
from galaxysim.engine.tick import run_ticks
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Building, Colony, Event, Fleet, IntentStatus, World
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

    # With a yard, the same order goes through.
    with open_session(engine) as session:
        civ = civ_by_name(session, universe_id, "Terrans")
        session.add(
            Building(colony_id=colony_id, kind="shipyard", work_remaining=0.0, completed_tick=0)
        )
        intents.build_fleet(session, civ, colony_id, 1.0, name="Second Wave")
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
        intents.build_structure(session, civ, colony.id, "laboratory")
        colony_id = colony.id

    run_ticks(engine, universe_id, 100)
    with open_session(engine) as session:
        lab = session.scalar(select(Building).where(Building.kind == "laboratory"))
        assert lab is not None, "foundations should still be laid"
        assert not lab.is_complete, "nobody is working on it"

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        intents.set_labor(session, colony, {INDUSTRY: 1.0})

    run_ticks(engine, universe_id, 100)
    with open_session(engine) as session:
        lab = session.scalar(select(Building).where(Building.kind == "laboratory"))
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
        yard = next(b for b in colony.buildings if b.kind == "shipyard")
        before, colony_id = yard.level, colony.id
        intents.build_structure(session, civ, colony.id, "shipyard")

    run_ticks(engine, universe_id, 200)

    with open_session(engine) as session:
        colony = session.get(Colony, colony_id)
        yards = [b for b in colony.buildings if b.kind == "shipyard"]
        assert len(yards) == 1, "an industry deepens rather than duplicating"
        assert yards[0].level == before + 1
        assert yards[0].is_complete, "and the level should have finished"


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
    """Enough of a World for :func:`assess`, which reads geology and nothing else."""

    def __init__(self, habitability: float, survey: dict) -> None:
        self.habitability = habitability
        self.survey = survey


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
        colony = _outpost(session, civ, habitability=0.9, stockpile={}, farmable=True)
        # A world that can supply its own chains end to end: ore for steel and
        # construction materials, ice for the water its people breathe. Starting
        # from an empty warehouse, everything it builds it has to dig up and
        # refine first -- which is the whole point of watching a governor run.
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
