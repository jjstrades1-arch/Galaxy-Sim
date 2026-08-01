"""The galaxy, and the scarcity that comes from where the players are.

Two families of property here.

**The galaxy is a pure function.** No map, no cap, no enumeration. Space is
generated from the universe seed and a position, so an unvisited system costs
nothing, computes identically everywhere, and stays the same forever. If that
breaks, star charts stop agreeing between clients and a saved game stops being
the same galaxy it was.

**Scarcity is a consequence of the seeding, not a mechanic.** Nobody fights when
there is land for everyone, and a hundred billion stars is land for everyone. So
the pressure comes from putting the players close together in one small region
and never growing it -- and the tests that matter are the ones that count what
is actually inside that region against how many people want it.
"""

from __future__ import annotations

import math
import random

import pytest
from sqlalchemy import select

from galaxysim.bootstrap import STARTING_CHARTED_SYSTEMS, add_civ, create_universe
from galaxysim.core.seeds import rng_for
from galaxysim.core.space import Vec3, distance
from galaxysim.model.base import create_engine_for, open_session
from galaxysim.model.entities import Colony, StarSystem, Universe, World
from galaxysim.worldgen.materialize import existing_system, materialize
from galaxysim.worldgen.galaxy import (
    ARM,
    CORE,
    DESIGN_POPULATION,
    REGIONS,
    RIM,
    SOLAR_RADIUS_LY,
    TARGET_SPACING_LY,
    arm_enhancement,
    frontier_centre,
    frontier_radius,
    metallicity_at,
    sector_of,
    seed_position,
    stellar_density,
    systems_in_sector,
    systems_near,
)
from galaxysim.worldgen.star import roll_star
from galaxysim.worldgen.survey import plausible_mass, plausible_orbits, survey_world


# --- the galaxy is a pure function -------------------------------------------


def test_a_sector_generates_identically_forever():
    """The property the whole design rests on.

    If asking twice gives different answers there is no galaxy, only a random
    number generator -- charts would disagree between clients and a system would
    change under a player who left and came back.
    """
    sector = (137, -42, 3)
    first = systems_in_sector(999, sector)
    second = systems_in_sector(999, sector)

    assert [s.key for s in first] == [s.key for s in second]
    assert [(s.position.x, s.position.y, s.position.z) for s in first] == [
        (s.position.x, s.position.y, s.position.z) for s in second
    ]


def test_different_universes_are_different_galaxies():
    sector = (137, -42, 3)
    assert systems_in_sector(1, sector) != systems_in_sector(2, sector)


def test_empty_space_costs_nothing():
    """Unvisited space must be free, or the galaxy cannot be unbounded.

    Deep rim sectors are almost always empty, and generating thousands of them
    should be instant -- there is nothing to generate.
    """
    empty = sum(len(systems_in_sector(5, (40_000, k, 0))) for k in range(500))
    assert empty == 0, "the far rim should be empty"


def test_a_system_belongs_to_the_sector_it_is_in():
    """Sector arithmetic has to round-trip or lazy lookup finds nothing."""
    for sector in ((0, 0, 0), (12, -3, 7), (-40, 900, -2)):
        for stub in systems_in_sector(11, sector):
            assert sector_of(stub.position) == sector


# --- the shape of it ---------------------------------------------------------


def test_the_solar_neighbourhood_matches_the_real_one():
    """One anchor, and it is measured rather than chosen.

    Stars near the Sun average about five light-years apart. Everything else --
    the core's crowding, the rim's emptiness -- is this figure times the
    profile, so if this drifts the whole galaxy drifts with it.
    """
    density = stellar_density(Vec3(SOLAR_RADIUS_LY, 0.0, 0.0))
    separation = density ** (-1.0 / 3.0)
    assert 4.0 < separation < 8.0, f"stars {separation:.1f} ly apart near the Sun"


def test_the_core_is_crowded_and_rich_and_the_rim_is_neither():
    """The trade the choice at join is made of.

    Density and metallicity run the same way, so you cannot have neighbours far
    away *and* good ore. That tension is the whole content of the decision.
    """
    core = Vec3(CORE.radius_ly, 0.0, 0.0)
    arm = Vec3(ARM.radius_ly, 0.0, 0.0)
    rim = Vec3(RIM.radius_ly, 0.0, 0.0)

    assert stellar_density(core) > stellar_density(arm) > stellar_density(rim)
    assert metallicity_at(core) > metallicity_at(arm) > metallicity_at(rim)

    # And by enough to be felt, not just enough to measure.
    assert stellar_density(core) / stellar_density(rim) > 20
    assert metallicity_at(core) - metallicity_at(rim) > 0.5


def test_the_galaxy_has_arms_and_they_are_ridges_rather_than_walls():
    """Arms are density waves. A star drifts through one; it cannot hit a wall."""
    radius = SOLAR_RADIUS_LY
    samples = [arm_enhancement(radius, a * math.pi / 180.0) for a in range(360)]

    assert max(samples) > 1.8, "arms should be a real enhancement"
    assert min(samples) == pytest.approx(1.0, abs=0.05), "interarm should be plain disc"
    # Smooth: no adjacent degree jumps by a large fraction of the contrast.
    steps = [abs(b - a) for a, b in zip(samples, samples[1:])]
    assert max(steps) < 0.2, "an arm edge would be a discontinuity"


def test_the_galaxy_has_no_edge():
    """Density falls off; it never stops. There is no wall to run into."""
    previous = float("inf")
    for radius in range(20_000, 90_000, 10_000):
        density = stellar_density(Vec3(float(radius), 0.0, 0.0))
        assert 0.0 < density < previous
        previous = density


def test_a_bounded_chart_does_not_pay_for_the_whole_core():
    """A player in the core has thirteen thousand stars within thirty light-years.

    Asking for the nearest twenty must not generate all of them, or the busiest
    place in the galaxy is the one the interface cannot show you.
    """
    centre = Vec3(CORE.radius_ly, 0.0, 0.0)
    nearest = systems_near(3, centre, 30.0, limit=20)

    assert len(nearest) == 20
    # Genuinely the nearest: each no further than the next.
    spans = [distance(s.position, centre) for s in nearest]
    assert spans == sorted(spans)

    # And they agree with the unbounded answer.
    full = systems_near(3, centre, 12.0)
    assert [s.key for s in systems_near(3, centre, 12.0, limit=5)] == [
        s.key for s in full[:5]
    ]


# --- the settlement frontier -------------------------------------------------


def _seat(players: int, region=ARM, seed: int = 7) -> list[Vec3]:
    taken: list[Vec3] = []
    for _ in range(players):
        taken.append(seed_position(seed, region, taken, expected_players=DESIGN_POPULATION))
    return taken


def _median_gap(positions: list[Vec3]) -> float:
    gaps = sorted(
        min(distance(p, q) for q in positions if q is not p) for p in positions
    )
    return gaps[len(gaps) // 2]


def test_the_frontier_is_sized_from_the_contact_target():
    """Derived, not picked.

    Seating the design population should leave neighbours about
    ``TARGET_SPACING_LY`` apart -- which is itself derived from wanting first
    contact in one to two weeks. If the placement rule changes, the packing
    constant has to be measured again, and this is what says so.
    """
    gap = _median_gap(_seat(DESIGN_POPULATION))
    assert gap == pytest.approx(TARGET_SPACING_LY, rel=0.15), (
        f"median neighbour gap {gap:.1f} ly against a {TARGET_SPACING_LY:.0f} ly target"
    )


def test_pressure_rises_with_population_on_its_own():
    """The mechanism that makes a full game a tense one.

    The frontier is sized once and never grows, so more players means tighter
    packing means sooner contact. Nothing enforces it -- it is what is left.
    """
    quiet = _median_gap(_seat(25))
    designed = _median_gap(_seat(DESIGN_POPULATION))
    crowded = _median_gap(_seat(300))

    assert quiet > designed > crowded
    assert quiet / crowded > 2.0, "a crowded frontier should feel entirely different"


def test_seating_is_reproducible():
    """A join has to be replayable or the universe is not deterministic."""
    assert [(p.x, p.y, p.z) for p in _seat(12)] == [(p.x, p.y, p.z) for p in _seat(12)]


def test_the_frontier_is_a_rounding_error_next_to_the_galaxy():
    """Everyone starts close enough to matter to each other.

    The point of a frontier is that the galaxy is *not* the playing area. If
    this ever grew to a meaningful fraction of the disc, players would never
    meet and there would be nothing to compete over.
    """
    assert frontier_radius(DESIGN_POPULATION) * 2 < 0.01 * 100_000


def test_every_region_is_somewhere_real():
    for region in REGIONS.values():
        centre = frontier_centre(13, region)
        assert stellar_density(centre) > 0
        assert distance(centre, Vec3(0, 0, 0)) == pytest.approx(
            region.radius_ly, rel=0.05
        )


# --- rows appear on arrival ---------------------------------------------------


def test_a_new_universe_generates_nothing(engine):
    """The galaxy costs nothing until somebody is in it.

    An empty universe is a seed and a region name. If ``create_universe`` ever
    starts pre-generating a starting region again, this fails -- and so does the
    claim that the galaxy is unbounded, because an unbounded thing you enumerate
    up front is a very large thing you enumerate up front.
    """
    create_universe(engine, "Empty", seed=99, region="arm")
    with open_session(engine) as session:
        assert session.scalars(select(StarSystem)).all() == []
        assert session.scalars(select(World)).all() == []


def test_seating_a_civilization_charts_its_neighbourhood(engine):
    """A species with starships knows the stars next door.

    Not the whole galaxy and not one system: the handful it could already point
    a telescope at. Everything beyond that is a journey.
    """
    universe_id = create_universe(engine, "Seated", seed=99, region="arm")
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        add_civ(session, universe, "Terrans")

    with open_session(engine) as session:
        charted = session.scalars(select(StarSystem).order_by(StarSystem.id)).all()
        assert len(charted) == STARTING_CHARTED_SYSTEMS
        assert all(system.worlds for system in charted)


def test_arriving_somewhere_new_brings_it_into_being(engine):
    """The moment a place stops being a computation and starts being a place."""
    universe_id = create_universe(engine, "Frontier", seed=99, region="arm")
    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        add_civ(session, universe, "Terrans")

    with open_session(engine) as session:
        universe = session.get(Universe, universe_id)
        home = session.scalars(select(Colony).order_by(Colony.id)).first()
        origin = home.world.system.position

        # Somewhere real, and somewhere nobody has been. Bounded, because an
        # unbounded sweep of a hundred light-years is two hundred thousand
        # systems and this is a test about one of them.
        far = [
            stub
            for stub in systems_near(universe.seed, origin, 100.0, limit=200)
            if existing_system(session, universe, stub) is None
        ]
        assert far, "the starting chart swallowed a hundred light-year sphere"
        target = far[-1]
        before = len(session.scalars(select(StarSystem)).all())

        system = materialize(session, universe, target)
        assert system.id is not None
        assert system.worlds, "a star with no planets is a bug, not a system"
        assert len(session.scalars(select(StarSystem)).all()) == before + 1

        # Twice is the same system, not a second one.
        assert materialize(session, universe, target).id == system.id
        assert len(session.scalars(select(StarSystem)).all()) == before + 1


def test_two_universes_reach_the_same_place_and_find_the_same_system(engine):
    """The pure-function property, verified through the database.

    Same seed, same sector, same index -- so the star, its name and its worlds
    have to come out identical no matter who arrives, from where, or when. This
    is what lets a system be stored as "a cache of a function plus history".
    """
    # Out in the disc: a sector this far from the centre holds a handful of
    # stars. The equivalent sector in the bulge holds a quarter of a million.
    stub = systems_in_sector(4242, (1_300, 0, 0))[0]

    def visit(url: str):
        eng = create_engine_for(url)
        uid = create_universe(eng, "Same", seed=4242, region="arm")
        with open_session(eng) as session:
            system = materialize(session, session.get(Universe, uid), stub)
            return (
                system.name,
                system.star_class,
                [(w.name, w.world_type, w.habitability) for w in system.worlds],
            )

    assert visit("sqlite://") == visit("sqlite://")


def test_the_region_you_choose_is_the_galaxy_you_get(engine):
    """The join-time decision, measured on the worlds it actually produces.

    Core starts are crowded and metal-rich; rim starts are neither. Both come
    out of the same generator -- the only difference is where in the disc the
    civilization was put, which is the whole point of the choice.
    """

    def seated(region: str):
        eng = create_engine_for("sqlite://")
        uid = create_universe(eng, region, seed=31337, region=region)
        with open_session(eng) as session:
            add_civ(session, session.get(Universe, uid), "Terrans")
        with open_session(eng) as session:
            systems = session.scalars(select(StarSystem).order_by(StarSystem.id)).all()
            origin = systems[0].position
            spread = max(distance(origin, s.position) for s in systems)
            return spread, metallicity_at(origin)

    core_spread, core_metals = seated("core")
    rim_spread, rim_metals = seated("rim")

    assert core_spread < rim_spread, "the core should pack the same stars far closer"
    assert core_metals > rim_metals + 0.5, "and be markedly richer in heavy elements"


# --- what is actually scarce -------------------------------------------------


def _sample_frontier_worlds(region, systems: int = 400):
    """Generate a spread of worlds from inside a region's frontier."""
    stubs = systems_near(7, frontier_centre(7, region), frontier_radius(DESIGN_POPULATION))
    picked = random.Random(99).sample(stubs, min(systems, len(stubs)))
    worlds = []
    for stub in picked:
        rng = rng_for(7, "sys", *stub.key)
        star = roll_star(rng)
        for orbit, au in enumerate(plausible_orbits(rng, star, rng.randint(1, 6))):
            world_rng = rng_for(7, "w", *stub.key, orbit)
            worlds.append(survey_world(world_rng, star, au, plausible_mass(world_rng, au, star)))
    return len(stubs), len(picked), worlds


def test_habitable_worlds_are_the_scarce_thing():
    """The number the whole competitive design rests on.

    A hundred civilizations, and the frontier holds a double-figure count of
    worlds anyone can breathe on -- every one of which is a place somebody else
    also wants, and everybody knows where it is. If this ever reaches a couple
    per player there is nothing to fight about and the game is a builder.
    """
    total, sampled, worlds = _sample_frontier_worlds(ARM)
    scale = total / sampled

    breathable = sum(1 for w in worlds if w.atmosphere.is_breathable) * scale
    per_player = breathable / DESIGN_POPULATION

    assert per_player < 1.0, (
        f"{breathable:,.0f} breathable worlds for {DESIGN_POPULATION} civs "
        f"({per_player:.2f} each) -- too many to fight over"
    )


def test_most_of_the_frontier_is_rock():
    """There is no shortage of *places*, only of good ones.

    That distinction is the design: a thousand systems per player and almost
    nothing among them worth having on its own terms, so what makes a world
    valuable is what is in it and where it sits.
    """
    total, sampled, worlds = _sample_frontier_worlds(ARM)
    assert total / DESIGN_POPULATION > 100, "the frontier should be roomy"

    liveable = sum(1 for w in worlds if w.habitability >= 0.25) / len(worlds)
    assert liveable < 0.01, f"{liveable:.1%} of worlds are liveable; should be rare"
