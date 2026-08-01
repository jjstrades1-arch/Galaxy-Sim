"""Creating a universe and seating civilizations in it.

The starting region is generated eagerly here. That is a placeholder: build-order
step 4 replaces ``seed_starting_region`` with the lazy
``system_at(universe_seed, sector)`` function, at which point a system only gets
a row when someone actually reaches it and the galaxy stops having a size at
all. The homeworld path stays, because a civ's starting system is by definition
visited.

Fair starts are enforced structurally, not by rerolling: a homeworld is always
rolled from :data:`HABITABLE_TYPES`, and every civ is seated on its own system.
"""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from galaxysim.core.resources import STARTING_STOCKPILE
from galaxysim.core.seeds import derive_seed, rng_for
from galaxysim.core.space import Vec3
from galaxysim.flavor.names import system_name, world_name
from galaxysim.model.base import init_db, open_session
from galaxysim.model.entities import (
    Civ,
    Colony,
    Fleet,
    StarSystem,
    Universe,
    UniverseMode,
    World,
)
from galaxysim.worldgen.types import HABITABLE_TYPES, roll_world, weighted_world_type

#: Radius of the eagerly generated starting region, in light-years.
STARTING_REGION_RADIUS_LY = 40.0

#: Colony pods on a civ's first fleet -- enough to plant a second colony without
#: waiting on production, so the first session has something to do.
STARTING_COLONY_PODS = 1
STARTING_FLEET_STRENGTH = 3.0


def create_universe(
    engine: Engine,
    name: str,
    *,
    seed: int | None = None,
    seconds_per_tick: int = 300,
    mode: UniverseMode = UniverseMode.SOLO,
    system_count: int = 12,
) -> int:
    """Create a universe with a starting region, returning its id."""
    init_db(engine)

    with open_session(engine) as session:
        universe = Universe(
            name=name,
            seed=seed if seed is not None else derive_seed("universe", name),
            seconds_per_tick=seconds_per_tick,
            mode=mode.value,
            tick_number=0,
        )
        session.add(universe)
        session.flush()

        seed_starting_region(session, universe, system_count)
        session.flush()
        return universe.id


def seed_starting_region(session: Session, universe: Universe, system_count: int) -> None:
    """Generate the opening cluster of systems.

    Placeholder for lazy generation (step 4). Positions and contents are derived
    from the universe seed, so the same seed always yields the same region.
    """
    for index in range(system_count):
        rng = rng_for(universe.seed, "system", index)
        radius = STARTING_REGION_RADIUS_LY * (rng.random() ** (1 / 3))
        position = _point_on_sphere(rng, radius)

        system = StarSystem(
            universe_id=universe.id,
            sector_i=int(position.x // 10),
            sector_j=int(position.y // 10),
            sector_k=int(position.z // 10),
            index_in_sector=index,
            name=system_name(rng),
            x=round(position.x, 6),
            y=round(position.y, 6),
            z=round(position.z, 6),
            star_class=rng.choice(("O", "B", "A", "F", "G", "K", "M", "M", "M")),
            discovered_tick=0,
        )
        session.add(system)
        session.flush()

        for orbit in range(rng.randint(1, 5)):
            world_rng = rng_for(universe.seed, "world", index, orbit)
            rolled = weighted_world_type(world_rng).roll(world_rng)
            session.add(
                World(
                    system_id=system.id,
                    name=world_name(world_rng, system.name, orbit),
                    world_type=rolled.world_type,
                    orbit_index=orbit,
                    habitability=rolled.habitability,
                    resource_yield=rolled.resource_yield,
                    hazard=rolled.hazard,
                )
            )


def add_civ(
    session: Session,
    universe: Universe,
    name: str,
    *,
    species_name: str = "",
    species_description: str = "",
    is_ai: bool = False,
) -> Civ:
    """Seat a new civilization on its own homeworld.

    Raises if no unclaimed system is left -- silently doubling up two civs on one
    system would be a much worse failure than refusing to start.
    """
    civ = Civ(
        universe_id=universe.id,
        name=name,
        species_name=species_name or name,
        species_description=species_description,
        is_ai=is_ai,
        seed=derive_seed(universe.seed, "civ", name),
        resources=dict(STARTING_STOCKPILE),
        research_points=0.0,
        research_invested=0.0,
        techs_known=0,
    )
    session.add(civ)
    session.flush()

    system = _claim_unoccupied_system(session, universe)
    rng = rng_for(civ.seed, "homeworld")
    homeworld = _prepare_homeworld(session, system, rng)

    session.add(
        Colony(
            world_id=homeworld.id,
            civ_id=civ.id,
            name=f"{homeworld.name} Prime",
            population=5.0,
            infrastructure=2.0,
            founded_tick=universe.tick_number,
        )
    )
    session.add(
        Fleet(
            universe_id=universe.id,
            civ_id=civ.id,
            name=f"{name} Expeditionary Fleet",
            strength=STARTING_FLEET_STRENGTH,
            colony_pods=STARTING_COLONY_PODS,
            # Every civ starts holding Lightspeed Travel -- there is no pre-FTL
            # game, so this is the floor rather than something to research up to.
            speed_ly_per_hour=1.0,
            x=system.x,
            y=system.y,
            z=system.z,
        )
    )
    session.flush()
    return civ


def _claim_unoccupied_system(session: Session, universe: Universe) -> StarSystem:
    """Lowest-id system that has no colony in it."""
    systems = session.scalars(
        select(StarSystem)
        .where(StarSystem.universe_id == universe.id)
        .order_by(StarSystem.id)
    ).all()

    for system in systems:
        if not any(world.colony is not None for world in system.worlds):
            return system

    raise RuntimeError(
        "no unoccupied system available for a new civilization; "
        "generate a larger starting region"
    )


def _prepare_homeworld(session: Session, system: StarSystem, rng) -> World:
    """Return a habitable world in ``system``, upgrading one if none qualifies.

    Nobody should open the game unable to grow, and rerolling the whole system
    would be a lot of churn to fix one stat -- so if the system has nothing
    habitable, the best candidate is re-rolled as a habitable type.
    """
    habitable = [w for w in sorted(system.worlds, key=lambda w: w.id) if w.habitability > 0]
    if habitable:
        return max(habitable, key=lambda w: (w.habitability, -w.id))

    candidate = sorted(system.worlds, key=lambda w: w.id)[0]
    rolled = roll_world(rng, rng.choice(HABITABLE_TYPES))
    candidate.world_type = rolled.world_type
    candidate.habitability = rolled.habitability
    candidate.hazard = rolled.hazard
    candidate.resource_yield = rolled.resource_yield
    session.flush()
    return candidate


def _point_on_sphere(rng, radius: float) -> Vec3:
    """A point at ``radius`` in a uniformly random direction."""
    # Sampling z uniformly and the angle uniformly gives an even spread over the
    # sphere; rolling two angles uniformly would cluster points at the poles.
    z = rng.uniform(-1.0, 1.0)
    theta = rng.uniform(0.0, 6.283185307179586)
    planar = (1.0 - z * z) ** 0.5
    from math import cos, sin

    return Vec3(radius * planar * cos(theta), radius * planar * sin(theta), radius * z)
