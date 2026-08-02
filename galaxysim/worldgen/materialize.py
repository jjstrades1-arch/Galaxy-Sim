"""Turning a place in space into rows in a database.

A system exists as soon as the galaxy function says it does. It becomes a *row*
the moment somebody arrives, and not before -- because that is the moment it
acquires state generation cannot derive: who owns it, what has been mined out,
who has a colony there.

Everything about a materialized system is derived from ``(universe_seed, sector,
index)``, so it does not matter who visits first or when. Two players who reach
the same system from opposite directions a month apart find the same star, the
same planets and the same ore. The row is a cache of a pure function plus
whatever has happened since.
"""

from __future__ import annotations


from sqlalchemy import select
from sqlalchemy.orm import Session

from galaxysim.core.seeds import rng_for
from galaxysim.core.space import Vec3
from galaxysim.flavor.names import system_name, world_name
from galaxysim.model.entities import StarSystem, Universe, World
from galaxysim.worldgen.galaxy import SystemStub, systems_near
from galaxysim.worldgen.serialize import promoted_fields, survey_to_json
from galaxysim.worldgen.star import roll_star
from galaxysim.worldgen.survey import plausible_mass, plausible_orbits, survey_world

#: How close a fleet has to be for a system to count as reached.
ARRIVAL_TOLERANCE_LY = 0.05


def existing_system(session: Session, universe: Universe, stub: SystemStub) -> StarSystem | None:
    """The row for this stub, if somebody has already been here."""
    return session.scalar(
        select(StarSystem).where(
            StarSystem.universe_id == universe.id,
            StarSystem.sector_i == stub.sector[0],
            StarSystem.sector_j == stub.sector[1],
            StarSystem.sector_k == stub.sector[2],
            StarSystem.index_in_sector == stub.index,
        )
    )


def materialize(session: Session, universe: Universe, stub: SystemStub) -> StarSystem:
    """Persist a system and its worlds, or return the row if it already exists.

    Idempotent by construction: the generation key is the sector and index, so
    arriving twice cannot produce two systems, and arriving late cannot produce
    a different one.
    """
    found = existing_system(session, universe, stub)
    if found is not None:
        return found

    rng = rng_for(universe.seed, "system", *stub.key)
    star = roll_star(rng)

    system = StarSystem(
        universe_id=universe.id,
        sector_i=stub.sector[0],
        sector_j=stub.sector[1],
        sector_k=stub.sector[2],
        index_in_sector=stub.index,
        name=system_name(rng),
        x=stub.position.x,
        y=stub.position.y,
        z=stub.position.z,
        star_class=star.designation,
        discovered_tick=universe.tick_number,
    )
    session.add(system)
    session.flush()

    # Orbits are laid out for the system as a whole rather than per world,
    # because planets in a real system are spaced against each other.
    for orbit_index, distance_au in enumerate(
        plausible_orbits(rng, star, rng.randint(2, 7))
    ):
        world_rng = rng_for(universe.seed, "world", *stub.key, orbit_index)
        survey = survey_world(
            world_rng, star, distance_au, plausible_mass(world_rng, distance_au, star)
        )
        session.add(world_from_survey(survey, system, world_rng, orbit_index))

    session.flush()
    return system


def world_from_survey(survey, system: StarSystem, rng, orbit_index: int) -> World:
    """Persist a generated world.

    The full physical description goes into the ``survey`` document; the numbers
    the engine queries or mutates are promoted to columns beside it.
    """
    return World(
        system_id=system.id,
        name=world_name(rng, system.name, orbit_index),
        world_type=survey.world_class,
        orbit_index=orbit_index,
        habitability=survey.habitability,
        # Hazard is a consequence of the world rather than its own roll:
        # volcanism, radiation where there is no magnetic field, and whatever
        # the local biology does to an unadapted coloniser.
        hazard=round(
            min(
                0.95,
                0.5 * survey.body.tectonic_activity
                + (0.0 if survey.body.is_shielded else 0.3)
                + 0.3 * survey.biosphere.pathogen_hazard,
            ),
            4,
        ),
        survey=survey_to_json(survey),
        land_area_km2=round(survey.land_area_km2, 2),
        carrying_capacity=round(survey.carrying_capacity, 2),
        **promoted_fields(survey),
    )


def materialize_around(
    session: Session, universe: Universe, position: Vec3, radius_ly: float, limit: int
) -> list[StarSystem]:
    """Bring the nearest systems around a point into being, nearest first.

    Used when a civilization is seated -- a species with lightspeed travel knows
    its own neighbourhood -- and whenever something needs a stretch of space to
    be real rather than merely computable.
    """
    return [
        materialize(session, universe, stub)
        for stub in systems_near(universe.seed, position, radius_ly, limit=limit)
    ]


def materialize_at(
    session: Session, universe: Universe, position: Vec3
) -> StarSystem | None:
    """The system a fleet has just arrived at, brought into being if needed.

    Returns ``None`` if the fleet is in empty space, which is a legal place to
    be: the galaxy is mostly empty and a course can end anywhere.
    """
    nearby = systems_near(universe.seed, position, ARRIVAL_TOLERANCE_LY, limit=1)
    if not nearby:
        return None
    return materialize(session, universe, nearby[0])
