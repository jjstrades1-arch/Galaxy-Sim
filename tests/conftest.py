"""Shared fixtures and helpers.

Tests run against in-memory SQLite. Because the models avoid dialect-specific
types, the same code paths exercise the Postgres deployment target.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, select

from galaxysim.bootstrap import add_civ, create_universe
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
    system_count: int = 12,
) -> int:
    """Create a universe with ``civs`` seated in it, returning its id."""
    universe_id = create_universe(
        engine,
        "Test Universe",
        seed=seed,
        seconds_per_tick=seconds_per_tick,
        mode=UniverseMode.SOLO,
        system_count=system_count,
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
                    round(civ.research_points, 6),
                    round(civ.research_invested, 6),
                    civ.techs_known,
                    tuple(sorted((k, round(v, 6)) for k, v in civ.resources.items())),
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
