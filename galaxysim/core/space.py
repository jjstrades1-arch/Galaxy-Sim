"""Continuous space.

Per the plan, the universe is not a graph of star systems joined by lanes. It is
continuous 3D space measured in light-years. A "system" is a gravitational
cluster of worlds that happens to sit at a position -- it groups things and
gives them a name, but it never gates movement. A fleet travels between two
points, and what it costs is a function of the distance, not of how many systems
lie between them.

The integer sector lattice laid over that space exists only for generation: it
gives :func:`galaxysim.worldgen.galaxy.systems_in_sector` a discrete key to
hash. It is not a tile the player moves across.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Edge length of one generation sector, in light-years.
SECTOR_SIZE_LY = 10.0

#: Decimal places positions are rounded to before they are stored or compared.
#: Float arithmetic is reproducible when the same operations run in the same
#: order, but rounding on write keeps stored state from accumulating noise that
#: would make two logically identical universes compare unequal.
POSITION_PRECISION = 6


@dataclass(frozen=True, slots=True)
class Vec3:
    """A position in continuous space, in light-years."""

    x: float
    y: float
    z: float

    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scalar: float) -> "Vec3":
        return Vec3(self.x * scalar, self.y * scalar, self.z * scalar)

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def normalized(self) -> "Vec3":
        """Unit vector in the same direction; the zero vector maps to itself."""
        magnitude = self.length()
        if magnitude == 0.0:
            return self
        return Vec3(self.x / magnitude, self.y / magnitude, self.z / magnitude)

    def quantized(self) -> "Vec3":
        """Round to :data:`POSITION_PRECISION`, for storage and comparison."""
        return Vec3(
            round(self.x, POSITION_PRECISION),
            round(self.y, POSITION_PRECISION),
            round(self.z, POSITION_PRECISION),
        )

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass(frozen=True, slots=True)
class SectorCoord:
    """Integer lattice cell used to key procedural generation."""

    i: int
    j: int
    k: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.i, self.j, self.k)

    def origin(self) -> Vec3:
        """Position of this sector's minimum corner."""
        return Vec3(
            self.i * SECTOR_SIZE_LY,
            self.j * SECTOR_SIZE_LY,
            self.k * SECTOR_SIZE_LY,
        )


def distance(a: Vec3, b: Vec3) -> float:
    """Straight-line distance in light-years."""
    return (a - b).length()


def sector_of(position: Vec3) -> SectorCoord:
    """Sector containing ``position``.

    Uses floor rather than truncation so the lattice stays uniform through the
    origin -- truncation would make the cells straddling zero twice as wide.
    """
    return SectorCoord(
        math.floor(position.x / SECTOR_SIZE_LY),
        math.floor(position.y / SECTOR_SIZE_LY),
        math.floor(position.z / SECTOR_SIZE_LY),
    )


def sectors_within(center: Vec3, radius_ly: float) -> list[SectorCoord]:
    """Every sector whose cell could intersect a sphere of ``radius_ly``.

    Returned in a fixed order so that callers iterating the result stay
    deterministic.
    """
    if radius_ly < 0:
        raise ValueError("radius must be non-negative")

    low = sector_of(Vec3(center.x - radius_ly, center.y - radius_ly, center.z - radius_ly))
    high = sector_of(Vec3(center.x + radius_ly, center.y + radius_ly, center.z + radius_ly))

    return [
        SectorCoord(i, j, k)
        for i in range(low.i, high.i + 1)
        for j in range(low.j, high.j + 1)
        for k in range(low.k, high.k + 1)
    ]


def travel_time_hours(origin: Vec3, destination: Vec3, speed_ly_per_hour: float) -> float:
    """Real-world hours a fleet needs to cross from ``origin`` to ``destination``.

    Travel is priced in wall-clock time, not ticks -- see the pacing rules in
    :mod:`galaxysim.engine.rates`. Speed comes from the fleet's drive and
    whatever tech effects modify it; every civ starts with Lightspeed Travel, so
    speed is never zero in practice.
    """
    if speed_ly_per_hour <= 0:
        raise ValueError("speed must be positive")
    return distance(origin, destination) / speed_ly_per_hour
