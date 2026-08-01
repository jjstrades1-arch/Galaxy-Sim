"""Shared fixtures and helpers.

Tests run against in-memory SQLite. Because the models avoid dialect-specific
types, the same code paths exercise the Postgres deployment target.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, select

from galaxysim.bootstrap import add_civ, create_universe
from galaxysim.engine.resolvers import queries
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import (
    Civ,
    Colony,
    Fleet,
    Intent,
    StarSystem,
    Universe,
    UniverseMode,
    World,
)


@pytest.fixture
def engine() -> Engine:
    # A file-backed URI would be closer to production, but in-memory keeps the
    # suite fast and each test gets a clean database.
    return create_engine_for("sqlite://")


def new_universe(
    engine: Engine,
    *,
    seed: int = 12345,
    seconds_per_tick: int = 300,
    civs: tuple[str, ...] = ("Terrans", "Vex"),
    system_count: int | None = None,
    region: str = "arm",
) -> int:
    """Create a universe with ``civs`` seated in it, returning its id.

    ``system_count`` is accepted and ignored. Nothing is generated eagerly any
    more -- each civ charts its own neighbourhood when it is seated, and
    everything beyond that materializes when a fleet arrives.
    """
    universe_id = create_universe(
        engine,
        "Test Universe",
        seed=seed,
        seconds_per_tick=seconds_per_tick,
        mode=UniverseMode.SOLO,
        region=region,
    )
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        assert universe is not None
        for name in civs:
            add_civ(session, universe, name)
    return universe_id


def civ_by_name(session, universe_id: int, name: str) -> Civ:
    civ = session.scalar(
        select(Civ).where(Civ.universe_id == universe_id, Civ.name == name)
    )
    assert civ is not None, f"no civ named {name}"
    return civ


def held(session, civ: Civ, resource: str) -> float:
    """How much of ``resource`` a civ holds across all its colonies.

    Reporting only -- see :func:`queries.total_stockpile`. Nothing spends from
    this; goods are spendable where they sit.
    """
    return queries.total_stockpile(session, civ.id).get(resource, 0.0)


#: Population of a test outpost: a landed expedition, in real people.
OUTPOST_POPULATION = 50_000.0


def rich_stockpile(amount: float = 1e9) -> dict[str, float]:
    """A stockpile holding plenty of everything.

    For tests about some *other* mechanic. With real materials, "can this colony
    pay for it" is a question with twenty-nine parts, and a test about labor
    allocation should not be quietly failing because the world has no bauxite.

    The default is deliberately enormous. Quantities are tonnes now, and a
    colony of fifty thousand people burns fifty of them an hour just breathing.
    """
    from galaxysim.materials import MATERIALS

    return {key: amount for key in MATERIALS}


def give_deposits(world: World, **yields: float) -> None:
    """Rewrite a world's geology so a test can say what comes out of it.

    There is no yield column to poke any more -- extraction reads the survey's
    real deposits -- so a test that wants "a world rich in iron" has to state it
    as geology. ``yields`` are yield *indices*, the one number extraction
    consumes: for reference a good iron world sits near 0.02 and a poor uranium
    one near 1e-5.

    Abundance carries the whole figure and the deposit is placed shallow at
    ordinary concentration, so the world reads as plausible if anyone prints it.
    """
    world.survey = dict(world.survey or {})
    world.survey["deposits"] = {
        element: {
            "element": element,
            "abundance": index,
            "concentration": 1.0,
            "depth": 0.0,
        }
        for element, index in sorted(yields.items())
    }
    refresh_promoted(world)


def refresh_promoted(world) -> None:
    """Recompute the columns derived from a world's survey.

    The engine reads extraction rates, surface water and farm quality off
    columns rather than re-parsing the survey document every tick. They are
    derived, so anything that rewrites a survey has to refresh them -- which in
    the game means terraforming, and in tests means these helpers.

    Tolerant of the partial surveys some tests build: what can be computed is.
    """
    from galaxysim.materials.extraction import extraction_rates
    from galaxysim.worldgen.geology import Deposit
    from galaxysim.worldgen.serialize import promoted_fields, survey_from_json

    survey = dict(getattr(world, "survey", None) or {})
    try:
        for field, value in promoted_fields(survey_from_json(survey)).items():
            setattr(world, field, value)
        return
    except (KeyError, TypeError):
        pass

    # Partial survey: at least keep the geology honest.
    deposits = {
        name: Deposit(**d) for name, d in sorted(survey.get("deposits", {}).items())
    }
    if hasattr(world, "extraction"):
        world.extraction = extraction_rates(deposits)


def clone_world(source: World, target: World) -> None:
    """Make ``target`` the same place as ``source``, physically.

    A test that compares two colonies has to put them on the same world, and
    "the same world" is more than the survey document: land area, carrying
    capacity, habitability and every promoted column derived from the survey
    all feed production. Copying the survey alone leaves the target mining at
    its own rates and farming at its own quality, which turns a controlled
    comparison into a comparison of two planets.

    In a generated galaxy the two free worlds a test happens to pick are never
    alike -- one may have nine times the land of the other -- so this is what
    makes "identical colonies" true rather than approximately true.
    """
    target.survey = dict(source.survey or {})
    target.world_type = source.world_type
    target.habitability = source.habitability
    target.hazard = source.hazard
    target.land_area_km2 = source.land_area_km2
    target.carrying_capacity = source.carrying_capacity
    refresh_promoted(target)


def make_farmable(world: World) -> None:
    """Give a test world a survey that agrees with a high habitability column.

    Tests used to set ``world.habitability = 1.0`` and be done. That worked while
    habitability was the only thing anything read. It is not any more:
    :mod:`galaxysim.colony.agriculture` reads the *survey* -- is there liquid
    water, is the native biochemistry edible, what is the gravity -- so a world
    can be nominally habitable and still be a hydroponics-only rock, and a test
    fixture that sets one without the other is quietly describing an impossible
    place.

    This writes the other half: oceans, an established compatible ecology, and a
    temperate climate at one gee.
    """
    survey = dict(world.survey or {})
    survey["hydrosphere"] = dict(
        survey.get("hydrosphere", {}),
        liquid_water=True,
        ocean_fraction=0.6,
        ice_fraction=0.05,
        mean_ocean_depth_km=3.0,
    )
    survey["biosphere"] = dict(
        survey.get("biosphere", {}),
        stage="complex",
        biochemistry="carbon-water",
        biomass_tonnes=2.0e12,
        pathogen_hazard=0.1,
        oxygenating=True,
    )
    survey["climate"] = dict(survey.get("climate", {}), surface_temp_k=288.0)
    survey["body"] = dict(survey.get("body", {}), gravity_g=1.0)
    world.survey = survey
    refresh_promoted(world)


def feed(colony, hours: float = 10_000.0) -> None:
    """Stock a colony with enough food to take eating off the table.

    For tests about water, labor or construction. Population needs food now, and
    a colony quietly starving in the background would fail those tests for a
    reason that has nothing to do with what they are checking.
    """
    from galaxysim.engine.rates import DEFAULT_RATES
    from galaxysim.materials import FOOD

    colony.stockpile[FOOD] = (
        colony.population * DEFAULT_RATES.food_per_person_per_hour * hours
    )


def take_manual_control(session, civ: Civ) -> None:
    """Switch every colony a civ holds to manual management.

    New colonies are governed by default, and a governor genuinely plays: it
    reassigns labor and spends the stockpile on buildings. Tests that exercise a
    mechanic by hand need to be the only thing touching the colony, so they take
    control first -- exactly as a player would.
    """
    for colony in queries.colonies_of(session, civ.id):
        colony.management_mode = "manual"


def home_colony(session, civ: Civ) -> Colony:
    """A civ's first colony, which is where bootstrap puts its starting goods."""
    colony = session.scalar(select(Colony).where(Colony.civ_id == civ.id).order_by(Colony.id))
    assert colony is not None, f"{civ.name} has no colonies"
    return colony


def snapshot(engine: Engine, universe_id: int) -> list[tuple]:
    """A comparable, fully ordered dump of everything a tick can change.

    Determinism tests compare these. Floats are rounded because the point is
    that two runs produce the same *state*, not that they produce bit-identical
    accumulations of float error -- though in practice they do.
    """
    rows: list[tuple] = []
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        assert universe is not None
        rows.append(("universe", universe.tick_number))

        for civ in session.scalars(
            select(Civ).where(Civ.universe_id == universe_id).order_by(Civ.id)
        ):
            rows.append(
                (
                    "civ",
                    civ.name,
                    round(civ.research_progress, 6),
                    round(civ.research_invested, 6),
                    civ.techs_known,
                )
            )

        for colony in session.scalars(select(Colony).order_by(Colony.id)):
            rows.append(
                (
                    "colony",
                    colony.name,
                    colony.civ_id,
                    round(colony.population, 6),
                    round(colony.infrastructure, 6),
                    colony.founded_tick,
                    tuple(sorted((k, round(v, 6)) for k, v in colony.stockpile.items())),
                )
            )

        for fleet in session.scalars(
            select(Fleet).where(Fleet.universe_id == universe_id).order_by(Fleet.id)
        ):
            rows.append(
                (
                    "fleet",
                    fleet.name,
                    fleet.civ_id,
                    round(fleet.strength, 6),
                    round(fleet.x, 6),
                    round(fleet.y, 6),
                    round(fleet.z, 6),
                    fleet.colony_pods,
                    fleet.arrival_tick,
                )
            )

        for intent in session.scalars(
            select(Intent).where(Intent.universe_id == universe_id).order_by(Intent.id)
        ):
            rows.append(("intent", intent.kind, intent.civ_id, intent.status, intent.resolved_tick))

        for system in session.scalars(
            select(StarSystem).where(StarSystem.universe_id == universe_id).order_by(StarSystem.id)
        ):
            rows.append(("system", system.name, round(system.x, 6), round(system.y, 6)))

        for world in session.scalars(select(World).order_by(World.id)):
            rows.append(
                ("world", world.name, world.world_type, round(world.habitability, 6))
            )

    return rows
