"""Creating a universe and seating civilizations in it.

The starting region is generated eagerly here. That is a placeholder: build-order
step 4 replaces ``seed_starting_region`` with the lazy
``system_at(universe_seed, sector)`` function, at which point a system only gets
a row when someone actually reaches it and the galaxy stops having a size at
all. The homeworld path stays, because a civ's starting system is by definition
visited.

Fair starts are enforced structurally: every civ is seated on its own system,
and its homeworld is searched for in the star's habitable zone until the physics
produces somewhere liveable. Genuinely habitable worlds are rare in this galaxy,
so a starting world is guaranteed -- but by generating one the model would
really produce, never by writing a habitability number over an unsuitable rock.
"""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from galaxysim.colony.industry import infrastructure_of, max_total_levels
from galaxysim.colony.labor import SECTORS, balanced_allocation
from galaxysim.engine.rates import DEFAULT_RATES
from galaxysim.materials import extraction_rates
from galaxysim.materials.catalogue import starting_stockpile
from galaxysim.core.seeds import derive_seed, rng_for
from galaxysim.core.space import Vec3
from galaxysim.flavor.names import system_name, world_name
from galaxysim.model.base import init_db, open_session
from galaxysim.model.entities import (
    Building,
    Civ,
    Colony,
    Fleet,
    StarSystem,
    Universe,
    UniverseMode,
    World,
)
from galaxysim.worldgen.serialize import deposits_from_json, promoted_fields, survey_to_json
from galaxysim.worldgen.star import Star, roll_star
from galaxysim.worldgen.survey import plausible_mass, plausible_orbits, survey_world

#: Radius of the eagerly generated starting region, in light-years.
STARTING_REGION_RADIUS_LY = 40.0

#: Colony pods on a civ's first fleet -- enough to plant a second colony without
#: waiting on production, so the first session has something to do.
STARTING_COLONY_PODS = 1
STARTING_FLEET_STRENGTH = 3.0

#: Industries the capital opens with, already finished. A homeworld is a place
#: with history, not a fresh landing -- and the shipyard in particular is
#: load-bearing, since without one a civ could never build its first ship.
STARTING_BUILDINGS: tuple[str, ...] = (
    "shipyard",
    "spaceport",
    "mine",
    "factory",
    "laboratory",
    "refinery",
    "granary",
)

#: How much of what the homeworld could support is already built. A
#: civilization of billions has been industrialising for a long time; opening it
#: with seven level-one buildings would describe a mining camp, not a capital.
#: Short of full on purpose, so there is still somewhere to put a surplus.
STARTING_DEVELOPMENT = 0.55

#: How full a homeworld starts, as a fraction of what the planet can hold.
#:
#: Very high on purpose. Logistic growth from 70% still adds four billion people
#: over a month, which is a larger contribution than several colonies and
#: undercuts the whole reason to expand. Starting nearly full means the capital
#: gains a few percent and then stops, so growth has to come from somewhere else
#: from the first hour -- which is the decision the opening position exists to
#: force.
HOMEWORLD_CAPACITY_FLOOR = 0.88
HOMEWORLD_CAPACITY_CEILING = 0.96


def _hourly_output(population: float, world: World, infrastructure: float) -> float:
    """Tonnes of ore this colony pulls in an hour at a balanced allocation.

    Used only to size the opening stockpile against the world it sits on. Reads
    the same deposits and the same rate production does, so the two cannot drift
    apart.
    """
    yields = world.extraction or {}
    if not yields:
        return 0.0
    share = 1.0 / len(SECTORS)
    return (
        sum(yields.values())
        * population
        * share
        * DEFAULT_RATES.extraction_per_worker_per_hour
        * infrastructure
    )


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

        star = roll_star(rng)
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
            star_class=star.designation,
            discovered_tick=0,
        )
        session.add(system)
        session.flush()

        # Orbits are laid out for the system as a whole, not per world, because
        # planets in a real system are spaced against each other.
        distances = plausible_orbits(rng, star, rng.randint(2, 7))
        for orbit_index, distance_au in enumerate(distances):
            world_rng = rng_for(universe.seed, "world", index, orbit_index)
            survey = survey_world(
                world_rng, star, distance_au, plausible_mass(world_rng, distance_au, star)
            )
            session.add(_world_from_survey(survey, system, world_rng, orbit_index))


def _world_from_survey(survey, system: StarSystem, rng, orbit_index: int) -> World:
    """Persist a generated world.

    The full physical description goes into the ``survey`` document; the few
    numbers the engine queries or mutates are promoted to columns beside it.
    """
    return World(
        system_id=system.id,
        name=world_name(rng, system.name, orbit_index),
        world_type=survey.world_class,
        orbit_index=orbit_index,
        habitability=survey.habitability,
        # Hazard is now a consequence of the world rather than its own roll:
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
        research_progress=0.0,
        research_invested=0.0,
        techs_known=0,
    )
    session.add(civ)
    session.flush()

    system = _claim_unoccupied_system(session, universe)
    rng = rng_for(civ.seed, "homeworld")
    homeworld = _prepare_homeworld(session, system, rng)

    # A species with lightspeed travel is not a landing party. Its homeworld is
    # already full -- seventy to ninety percent of what the planet can hold --
    # so it barely grows, and essentially all growth has to come from expanding.
    # That is the decision the opening position exists to force.
    infrastructure = 2.0
    population = homeworld.carrying_capacity * rng.uniform(
        HOMEWORLD_CAPACITY_FLOOR, HOMEWORLD_CAPACITY_CEILING
    )

    capital = Colony(
        world_id=homeworld.id,
        civ_id=civ.id,
        name=f"{homeworld.name} Prime",
        population=population,
        infrastructure=infrastructure,
        founded_tick=universe.tick_number,
        # The starting stockpile sits on the homeworld rather than in a
        # civ-wide treasury: everything a civ owns is somewhere. Sized from what
        # this particular world produces, so a big homeworld opens rich and a
        # cramped one does not.
        stockpile=starting_stockpile(_hourly_output(population, homeworld, infrastructure)),
        labor=balanced_allocation(),
    )
    session.add(capital)
    session.flush()

    # The capital opens with infrastructure a colony would otherwise have to
    # build. The shipyard matters most: without one a colony cannot build ships
    # at all, so a civ with no starting yard could never build its first fleet
    # and would have no way out of the opening position.
    # Industries, at a level the population can actually staff. Splitting the
    # world's whole capacity evenly across them is crude but honest: what it
    # gets right is the *scale*, which is what makes a capital feel like a
    # capital rather than an outpost with a better address.
    ceiling = max_total_levels(population, homeworld.land_area_km2)
    level = max(1, int(ceiling * STARTING_DEVELOPMENT / len(STARTING_BUILDINGS)))
    for kind in STARTING_BUILDINGS:
        session.add(
            Building(
                colony_id=capital.id,
                kind=kind,
                level=level,
                work_remaining=0.0,
                started_tick=universe.tick_number,
                completed_tick=universe.tick_number,
            )
        )
    session.flush()
    capital.infrastructure += infrastructure_of(capital.buildings)
    capital.development = min(1.0, len(STARTING_BUILDINGS) * level / max(1, ceiling))
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
            cargo_capacity=STARTING_FLEET_STRENGTH * 20.0,
            cargo={},
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


#: Habitability a starting world must reach. Genuinely habitable worlds are
#: rare in this galaxy by design, so a civ's own homeworld is guaranteed rather
#: than left to chance -- nobody should open the game unable to grow.
HOMEWORLD_MIN_HABITABILITY = 0.55


def _life_bearing_star(rng) -> "Star":
    """A star of the kind civilizations actually arise around.

    Not a cheat so much as the anthropic principle applied directly: a species
    exists to play this game because its homeworld had breathable air, which
    required photosynthetic life, which required billions of stable years. That
    rules out most of the galaxy's stars -- the three-quarters that are M dwarfs
    tidally lock their habitable zones, and young stars have not had time.

    So a starting system gets a mature F, G or K star. Every *other* system in
    the galaxy still rolls on the real distribution.
    """
    while True:
        star = roll_star(rng)
        if star.spectral_class not in ("F", "G", "K") or star.age_gyr < 3.5:
            continue
        # Bright enough that its habitable zone is far enough out to escape
        # tidal locking. A dim K8 dwarf passes the class test and still puts its
        # habitable zone at a quarter of an AU, where worlds lock into a
        # permanent day face and a frozen night face -- which is exactly the
        # objection astronomers raise to late-K and M dwarf habitability.
        if star.luminosity_solar >= 0.2:
            return star


def _prepare_homeworld(session: Session, system: StarSystem, rng) -> World:
    """Give ``system`` a life-bearing star and find a world to start on.

    The guarantee is implemented by *searching for a world the generator would
    genuinely produce*, never by writing a habitability number over an
    unsuitable rock. A homeworld's survey therefore reads like any other
    world's, and everything on it is true.

    A breathable atmosphere is the hard requirement, because habitability is
    capped low without one -- and a breathable atmosphere means an oxygenating
    biosphere, which means the world has native life. That is the right story
    for a species' place of origin.
    """
    # Derive the search seed from the caller's RNG, which descends from the
    # universe seed. Keying on system.id alone would give every universe the
    # same homeworld for the same row id.
    base = rng.getrandbits(48)

    star = _life_bearing_star(rng)
    system.star_class = star.designation
    inner, outer = star.habitable_zone

    ordered = sorted(system.worlds, key=lambda w: w.id)
    home_index = ordered[0].orbit_index if ordered else 0

    # Regenerate the whole system around its new star, so the other worlds stay
    # consistent with the sun they orbit.
    distances = plausible_orbits(rng, star, max(len(ordered), 3))
    for world, distance in zip(ordered, distances):
        world_rng = rng_for(base, "reseed", world.orbit_index)
        survey = survey_world(
            world_rng, star, distance, plausible_mass(world_rng, distance, star)
        )
        _apply_survey(world, _world_from_survey(survey, system, world_rng, world.orbit_index))

    # Now search the habitable zone for somewhere worth being born.
    # Ranked on breathability first, then habitability. Ranking on habitability
    # alone loses a breathable world to a marginally prettier unbreathable one,
    # and breathable air is the thing that actually makes it a homeworld.
    chosen = None
    chosen_rank = (-1, -1.0)
    for attempt in range(3000):
        attempt_rng = rng_for(base, "homeworld", attempt)
        distance = attempt_rng.uniform(inner, outer)
        mass = attempt_rng.uniform(0.75, 1.5)
        survey = survey_world(attempt_rng, star, distance, mass)

        rank = (1 if survey.atmosphere.is_breathable else 0, survey.habitability)
        if rank > chosen_rank:
            chosen, chosen_rank = survey, rank
        if rank[0] and survey.habitability >= HOMEWORLD_MIN_HABITABILITY:
            break

    assert chosen is not None
    homeworld = ordered[0] if ordered else None
    assert homeworld is not None, "a system must have worlds before it can be settled"
    _apply_survey(homeworld, _world_from_survey(chosen, system, rng, home_index))
    session.flush()
    return homeworld


def _apply_survey(world: World, generated: World) -> None:
    """Copy a freshly generated world's fields onto an existing row."""
    world.name = generated.name
    world.world_type = generated.world_type
    world.habitability = generated.habitability
    world.hazard = generated.hazard
    world.survey = generated.survey
    world.land_area_km2 = generated.land_area_km2
    world.carrying_capacity = generated.carrying_capacity
    world.extraction = generated.extraction
    world.surface_water = generated.surface_water
    world.farm_quality = generated.farm_quality
    world.needs_fertiliser = generated.needs_fertiliser


def _point_on_sphere(rng, radius: float) -> Vec3:
    """A point at ``radius`` in a uniformly random direction."""
    # Sampling z uniformly and the angle uniformly gives an even spread over the
    # sphere; rolling two angles uniformly would cluster points at the poles.
    z = rng.uniform(-1.0, 1.0)
    theta = rng.uniform(0.0, 6.283185307179586)
    planar = (1.0 - z * z) ** 0.5
    from math import cos, sin

    return Vec3(radius * planar * cos(theta), radius * planar * sin(theta), radius * z)
