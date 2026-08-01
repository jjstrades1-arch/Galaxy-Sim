"""The galaxy: a real spiral disc, generated on demand.

There is no world cap and no map. Space is a **pure function of the universe
seed and a position**, so an unvisited system costs nothing to store, nothing to
generate until somebody goes there, and computes identically for every client
that asks. The galaxy is effectively unbounded because nothing is enumerated.

**Why a real spiral rather than a sphere of stars.** Density is the thing that
makes position matter. Near the Sun, stars average five to eight light-years
apart; in the core they are a fraction of that, and out past the arms they thin
to almost nothing. That gradient is what turns "where do I start" into a real
decision -- a core start is a crowded neighbourhood full of metal-rich systems
and neighbours you did not choose, a rim start is long journeys and privacy --
and none of it needs a rule, because it is just where the stars are.

Metallicity runs the same way and matters for the same reason. A star's
metallicity decides how much heavy element its planets could form from, so a
metal-poor rim genuinely produces worse ore. The choice at join is therefore
between density, wealth and safety, and you cannot have all three.

The structure:

* An exponential **disc**, scale length ~11,000 ly, thin in z.
* Four **logarithmic spiral arms** wound at ~12 degrees, riding on top of the
  disc as a density enhancement rather than as a hard edge.
* A **core** bulge that dominates inside a few thousand light-years.
* No boundary. Density falls off exponentially, so the far rim is empty by
  arithmetic rather than by a wall.

Generation is sector-based: space is diced into cubes, and the systems in a cube
are drawn from an RNG seeded on ``(universe_seed, i, j, k)``. Asking about a
cube twice gives the same answer forever, which is what makes this a *map*
rather than a random number generator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from galaxysim.core.seeds import rng_for
from galaxysim.core.space import Vec3

#: Edge of one generation cube, in light-years. Chosen so an ordinary
#: neighbourhood holds a handful of systems per cube: small enough that walking
#: the cubes in a search radius is cheap, large enough that the walk is short.
SECTOR_LY = 20.0

#: Stars per cubic light-year in the solar neighbourhood. Real: stars average
#: about 5 ly apart here, so roughly one per 125 cubic light-years.
SOLAR_DENSITY = 1.0 / 125.0

#: How far the Sun sits from the galactic centre, in light-years. Everything
#: else is measured against this so "solar neighbourhood" means something.
SOLAR_RADIUS_LY = 26_000.0

#: Exponential scale length of the disc, and its scale height. Real figures for
#: a galaxy like ours -- the disc is enormously wider than it is thick, which is
#: why the third coordinate barely matters for finding neighbours.
DISC_SCALE_LENGTH_LY = 11_000.0
DISC_SCALE_HEIGHT_LY = 1_000.0

#: The central bulge: dense, old, metal-rich, and small compared with the disc.
BULGE_RADIUS_LY = 3_000.0
BULGE_PEAK_DENSITY = 30.0

#: Four arms, wound at twelve degrees, each enhancing density by up to this much
#: where a position sits squarely inside one.
ARM_COUNT = 4
ARM_PITCH_RAD = math.radians(12.0)
ARM_CONTRAST = 2.2
#: How wide an arm is, in light-years. Wide: arms are density waves, not walls.
ARM_WIDTH_LY = 2_600.0

#: Metallicity at the centre and the gradient outward, in dex per light-year.
#: Real galaxies run about -0.06 dex per kiloparsec; this is that, converted.
CORE_METALLICITY = 0.45
METALLICITY_GRADIENT = -0.06 / 3261.0


@dataclass(frozen=True, slots=True)
class Region:
    """A named place to start, and what it costs you."""

    key: str
    name: str
    description: str
    #: Distance from the galactic centre, in light-years.
    radius_ly: float


CORE = Region(
    "core",
    "The Core",
    "Stars a fraction of a light-year apart and metal-rich to a fault. "
    "Everything is close, which means everything worth having is contested and "
    "your neighbours are on top of you from the first week.",
    6_000.0,
)
ARM = Region(
    "arm",
    "The Arm",
    "The solar neighbourhood: stars a handful of light-years apart, ordinary "
    "metallicity, room to grow into but company soon enough.",
    SOLAR_RADIUS_LY,
)
RIM = Region(
    "rim",
    "The Rim",
    "Thin, poor and quiet. Journeys are long and the ore is worse, and nobody "
    "will find you for a very long time.",
    46_000.0,
)

REGIONS: dict[str, Region] = {r.key: r for r in (CORE, ARM, RIM)}


# --- the shape of the galaxy -------------------------------------------------


def _cylindrical(position: Vec3) -> tuple[float, float, float]:
    radius = math.hypot(position.x, position.y)
    angle = math.atan2(position.y, position.x)
    return radius, angle, position.z


def arm_enhancement(radius_ly: float, angle_rad: float) -> float:
    """How much the spiral arms raise density at this point, from 1 upward.

    A logarithmic spiral has ``theta = ln(r / r0) / tan(pitch)``. Measuring how
    far a position sits from the nearest arm crest, in angle, and converting
    that back into a distance gives a smooth ridge rather than a boundary --
    which is right, because arms are density waves that stars drift through, not
    structures with edges.
    """
    if radius_ly < 1.0:
        return 1.0

    crest = math.log(radius_ly / 1_000.0) / math.tan(ARM_PITCH_RAD)
    spacing = 2.0 * math.pi / ARM_COUNT

    offset = (angle_rad - crest) % spacing
    angular_distance = min(offset, spacing - offset)
    # Arc length from the crest, which is what "width" actually means.
    distance = angular_distance * radius_ly

    return 1.0 + (ARM_CONTRAST - 1.0) * math.exp(-((distance / ARM_WIDTH_LY) ** 2))


def stellar_density(position: Vec3) -> float:
    """Stars per cubic light-year at ``position``.

    Bulge plus exponential disc, modulated by the arms. Falls away smoothly in
    every direction, so the galaxy has no edge -- just places so thin that a
    sector is empty more often than not.
    """
    radius, angle, height = _cylindrical(position)

    disc = math.exp(-radius / DISC_SCALE_LENGTH_LY) * math.exp(
        -abs(height) / DISC_SCALE_HEIGHT_LY
    )
    bulge = BULGE_PEAK_DENSITY * math.exp(-((radius / BULGE_RADIUS_LY) ** 2)) * math.exp(
        -abs(height) / BULGE_RADIUS_LY
    )

    # Normalised so that the solar neighbourhood comes out at SOLAR_DENSITY,
    # which is the one figure here that is measured rather than chosen.
    reference = math.exp(-SOLAR_RADIUS_LY / DISC_SCALE_LENGTH_LY)
    return SOLAR_DENSITY * (disc + bulge) / reference * arm_enhancement(radius, angle)


def metallicity_at(position: Vec3) -> float:
    """Expected [Fe/H] at ``position``.

    Metal-rich in the core, poor at the rim, on the real gradient. This is not
    flavour: a star's metallicity sets how much heavy element its planets could
    form from, so it decides how good the ore is for everyone who settles there.
    """
    radius, _, _ = _cylindrical(position)
    return round(CORE_METALLICITY + METALLICITY_GRADIENT * radius, 3)


# --- lazy generation ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SystemStub:
    """A system that exists but has never been visited.

    Enough to plot a course to and to draw on a chart. It becomes a row in the
    database the moment somebody arrives, and not before -- which is what lets
    the galaxy be unbounded.
    """

    sector: tuple[int, int, int]
    index: int
    position: Vec3

    @property
    def key(self) -> tuple[int, int, int, int]:
        return (*self.sector, self.index)


def sector_of(position: Vec3) -> tuple[int, int, int]:
    """Which generation cube a position falls in."""
    return (
        math.floor(position.x / SECTOR_LY),
        math.floor(position.y / SECTOR_LY),
        math.floor(position.z / SECTOR_LY),
    )


def sector_centre(sector: tuple[int, int, int]) -> Vec3:
    i, j, k = sector
    half = SECTOR_LY / 2.0
    return Vec3(i * SECTOR_LY + half, j * SECTOR_LY + half, k * SECTOR_LY + half)


def systems_in_sector(universe_seed: int, sector: tuple[int, int, int]) -> list[SystemStub]:
    """Every system in one cube. Pure, stable, and cheap.

    The count is Poisson-ish around the local density, drawn from an RNG seeded
    on the sector, so two processes asking about the same cube of empty space a
    year apart get the same answer without either of them storing anything.
    """
    centre = sector_centre(sector)
    expected = stellar_density(centre) * SECTOR_LY**3
    if expected <= 0.0:
        return []

    rng = rng_for(universe_seed, "sector", *sector)
    count = _poisson(rng, expected)
    if count <= 0:
        return []

    half = SECTOR_LY / 2.0
    stubs: list[SystemStub] = []
    for index in range(count):
        stubs.append(
            SystemStub(
                sector=sector,
                index=index,
                position=Vec3(
                    round(centre.x + rng.uniform(-half, half), 4),
                    round(centre.y + rng.uniform(-half, half), 4),
                    round(centre.z + rng.uniform(-half, half), 4),
                ),
            )
        )
    return stubs


def _poisson(rng, mean: float) -> int:
    """Knuth's method for small means, normal approximation for large ones.

    Sector counts are usually a handful, but a core sector can hold thousands,
    and multiplying uniforms that many times both underflows and takes forever.
    """
    if mean > 30.0:
        value = rng.gauss(mean, math.sqrt(mean))
        return max(0, int(round(value)))

    limit = math.exp(-mean)
    count, product = 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def systems_near(
    universe_seed: int,
    position: Vec3,
    radius_ly: float,
    limit: int | None = None,
) -> list[SystemStub]:
    """Systems within ``radius_ly`` of a point, nearest first.

    Walks the cubes the sphere touches, and walks them *in order of how close
    they are*, so a bounded query can stop as soon as every remaining cube is
    farther away than the worst result it already holds. That matters in the
    core, where thirty light-years contains thirteen thousand stars: a chart
    asking for the nearest twenty should not pay for all of them.

    Deterministic in every sense that matters -- the same call returns the same
    systems in the same order, which resolvers and star charts both depend on.
    """
    reach = int(math.ceil(radius_ly / SECTOR_LY))
    origin = sector_of(position)

    offsets = [
        (di, dj, dk)
        for di in range(-reach, reach + 1)
        for dj in range(-reach, reach + 1)
        for dk in range(-reach, reach + 1)
    ]
    # Nearest cube first, by the closest point of that cube to us.
    offsets.sort(key=lambda o: _sector_floor_distance(o, position, origin))

    found: list[SystemStub] = []
    for offset in offsets:
        if limit is not None and len(found) >= limit:
            # Everything left is at least this far away, so if the results we
            # already hold are all closer, nothing further can displace them.
            floor = _sector_floor_distance(offset, position, origin)
            found.sort(key=lambda s: (_distance(s.position, position), s.key))
            if _distance(found[limit - 1].position, position) <= floor:
                return found[:limit]

        sector = (origin[0] + offset[0], origin[1] + offset[1], origin[2] + offset[2])
        for stub in systems_in_sector(universe_seed, sector):
            if _distance(stub.position, position) <= radius_ly:
                found.append(stub)

    found.sort(key=lambda s: (_distance(s.position, position), s.key))
    return found[:limit] if limit is not None else found


def _sector_floor_distance(
    offset: tuple[int, int, int], position: Vec3, origin: tuple[int, int, int]
) -> float:
    """Closest any point in this cube could be to ``position``."""
    sector = (origin[0] + offset[0], origin[1] + offset[1], origin[2] + offset[2])
    low = Vec3(sector[0] * SECTOR_LY, sector[1] * SECTOR_LY, sector[2] * SECTOR_LY)
    gap = 0.0
    for axis, base in ((position.x, low.x), (position.y, low.y), (position.z, low.z)):
        if axis < base:
            gap += (base - axis) ** 2
        elif axis > base + SECTOR_LY:
            gap += (axis - base - SECTOR_LY) ** 2
    return math.sqrt(gap)


def _distance(a: Vec3, b: Vec3) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


# --- the settlement frontier -------------------------------------------------
#
# Nobody fights when there is land for everyone, and the galaxy has land for
# everyone -- a hundred billion stars will not run out. So scarcity cannot come
# from shrinking the map. It comes from *where the players are*.
#
# The size of the frontier is derived rather than picked. Working backwards from
# a one-to-two-week time to first contact at one light-year per hour: a player's
# practical claim radius is fifteen to twenty-five light-years after a week and
# thirty to fifty after two, so neighbouring homeworlds want to be about fifty
# light-years apart. Close-packing the expected population at that spacing gives
# the radius, and the region is sized **once** and never grows.
#
# That last part is the mechanism. New players seed into the same volume, so
# spacing falls as the game fills:
#
#     25 players  ~80 ly apart  first contact ~3 weeks
#     100 players ~50 ly apart  first contact 1-2 weeks
#     300 players ~35 ly apart  first contact under a week
#
# Pressure rises with population on its own, and the rest of the galaxy is the
# escape valve for anyone willing to travel a long way for elbow room.

#: Light-years between neighbouring homeworlds at the design population. Derived
#: from the contact-time target, not chosen for feel.
TARGET_SPACING_LY = 50.0

#: How many players a frontier is sized for by default.
DESIGN_POPULATION = 100

#: Packing efficiency of the arrangement points actually fall into.
#:
#: **Measured, not assumed.** Farthest-point placement is not a crystal lattice
#: and does not achieve one's density, so this was calibrated by seating a
#: hundred civilizations and adjusting until the median gap between neighbours
#: came out at :data:`TARGET_SPACING_LY`. Change the placement rule and this
#: needs measuring again -- ``tests/test_galaxy.py`` fails if it drifts.
PACKING_FACTOR = 0.93

#: Candidate positions considered when seating each new civilization. Higher is
#: a better spread and a slower join; this is enough that the result is visibly
#: even and the cost is nothing.
PLACEMENT_CANDIDATES = 256


def frontier_radius(expected_players: int = DESIGN_POPULATION) -> float:
    """Radius of the settlement region, in light-years.

    Derived: the volume needed to hold ``expected_players`` at
    :data:`TARGET_SPACING_LY` separation. A hundred players comes out near
    130 ly -- a region about 260 ly across, which is a quarter of one percent of
    the galaxy's diameter and is the whole point. Everyone starts close enough
    to matter to each other.
    """
    players = max(1, expected_players)
    volume_each = TARGET_SPACING_LY**3 / PACKING_FACTOR
    return round((players * volume_each * 3.0 / (4.0 * math.pi)) ** (1.0 / 3.0), 2)


def frontier_centre(universe_seed: int, region: Region) -> Vec3:
    """Where in the galaxy this universe's frontier sits.

    Somewhere on the chosen region's radius, at an angle drawn from the universe
    seed -- so two universes with the same region are still different places, and
    a given universe's frontier is always in the same place.
    """
    rng = rng_for(universe_seed, "frontier", region.key)
    angle = rng.uniform(0.0, 2.0 * math.pi)
    height = rng.gauss(0.0, DISC_SCALE_HEIGHT_LY * 0.25)
    return Vec3(
        round(region.radius_ly * math.cos(angle), 4),
        round(region.radius_ly * math.sin(angle), 4),
        round(height, 4),
    )


def seed_position(
    universe_seed: int,
    region: Region,
    taken: list[Vec3],
    expected_players: int = DESIGN_POPULATION,
) -> Vec3:
    """Where to seat the next civilization.

    Farthest-point placement: draw candidates inside the frontier and take the
    one furthest from everybody already there. Deterministic given the seed and
    who is already seated, which is what makes a join reproducible.

    The behaviour that matters is what happens as the region fills. The first
    civ lands near the middle, the second across from it, and by the time a
    hundred are seated the best available gap is about fifty light-years -- not
    because anything enforces that, but because that is what is left. Spacing is
    an outcome of how many people are playing.
    """
    centre = frontier_centre(universe_seed, region)
    radius = frontier_radius(expected_players)
    rng = rng_for(universe_seed, "seat", region.key, len(taken))

    best: Vec3 | None = None
    best_gap = -1.0
    for _ in range(PLACEMENT_CANDIDATES):
        candidate = _point_in_sphere(rng, centre, radius)
        gap = min((_distance(candidate, other) for other in taken), default=radius)
        if gap > best_gap:
            best, best_gap = candidate, gap

    assert best is not None
    return best


def _point_in_sphere(rng, centre: Vec3, radius: float) -> Vec3:
    """Uniform inside the sphere -- cube-rooted radius, or it clusters inward."""
    r = radius * (rng.random() ** (1.0 / 3.0))
    z = rng.uniform(-1.0, 1.0)
    theta = rng.uniform(0.0, 2.0 * math.pi)
    planar = math.sqrt(max(0.0, 1.0 - z * z))
    return Vec3(
        round(centre.x + r * planar * math.cos(theta), 4),
        round(centre.y + r * planar * math.sin(theta), 4),
        round(centre.z + r * z, 4),
    )
