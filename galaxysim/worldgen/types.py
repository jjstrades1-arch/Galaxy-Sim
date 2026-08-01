"""The world type table.

This is the one part of world generation that is *not* procedural. The set of
world types is fixed and hand-authored, and generation only rolls stats within
each type's declared ranges. That split is what keeps procedural worlds
trustworthy: a player who learns what an ocean world is worth has learned
something durable, and no roll can produce a world that breaks the economy.

Ranges are deliberately overlapping. A good desert world should beat a poor
ocean world, so that scouting is worth doing and "find the best type" is not the
entire exploration game.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from galaxysim.core.resources import ENERGY, METAL, VOLATILES

Range = tuple[float, float]


@dataclass(frozen=True, slots=True)
class WorldType:
    """A class of world, with the bounds generation may roll within."""

    name: str
    habitability: Range
    hazard: Range
    #: Resource -> the range its per-hour yield multiplier may roll within.
    yields: dict[str, Range] = field(default_factory=dict)
    #: How many structures a world of this type can host. Cramped and hostile
    #: worlds support fewer, so the richest worlds are also the ones where you
    #: must choose what they are *for*.
    slots: tuple[int, int] = (3, 5)
    #: Relative frequency when picking a type for a fresh world.
    weight: float = 1.0

    def roll(self, rng: random.Random) -> "RolledWorld":
        """Roll concrete stats within this type's bounds."""
        return RolledWorld(
            world_type=self.name,
            habitability=round(rng.uniform(*self.habitability), 4),
            hazard=round(rng.uniform(*self.hazard), 4),
            # Sorted so the draw order does not depend on dict iteration order.
            resource_yield={
                resource: round(rng.uniform(*bounds), 4)
                for resource, bounds in sorted(self.yields.items())
            },
            slots=rng.randint(*self.slots),
        )


@dataclass(frozen=True, slots=True)
class RolledWorld:
    """Concrete stats for one generated world, before it is persisted."""

    world_type: str
    habitability: float
    hazard: float
    resource_yield: dict[str, float]
    slots: int = 4


WORLD_TYPES: tuple[WorldType, ...] = (
    WorldType(
        name="terrestrial",
        habitability=(0.55, 1.0),
        hazard=(0.0, 0.15),
        yields={METAL: (0.4, 0.9), ENERGY: (0.3, 0.7), VOLATILES: (0.3, 0.8)},
        slots=(5, 8),
        weight=0.8,
    ),
    WorldType(
        name="ocean",
        habitability=(0.45, 0.9),
        hazard=(0.05, 0.2),
        yields={METAL: (0.2, 0.5), ENERGY: (0.3, 0.6), VOLATILES: (0.6, 1.2)},
        slots=(4, 7),
        weight=0.7,
    ),
    WorldType(
        name="desert",
        habitability=(0.2, 0.6),
        hazard=(0.1, 0.35),
        yields={METAL: (0.6, 1.1), ENERGY: (0.6, 1.0), VOLATILES: (0.05, 0.25)},
        slots=(4, 7),
        weight=1.0,
    ),
    WorldType(
        name="ice",
        habitability=(0.1, 0.45),
        hazard=(0.15, 0.4),
        yields={METAL: (0.3, 0.7), ENERGY: (0.1, 0.3), VOLATILES: (0.7, 1.3)},
        slots=(3, 6),
        weight=1.0,
    ),
    WorldType(
        name="volcanic",
        habitability=(0.05, 0.3),
        hazard=(0.35, 0.7),
        yields={METAL: (1.0, 1.8), ENERGY: (0.9, 1.6), VOLATILES: (0.1, 0.3)},
        slots=(3, 5),
        weight=0.8,
    ),
    WorldType(
        name="toxic",
        habitability=(0.0, 0.2),
        hazard=(0.5, 0.85),
        yields={METAL: (0.5, 1.0), ENERGY: (0.3, 0.7), VOLATILES: (0.9, 1.7)},
        slots=(3, 5),
        weight=0.7,
    ),
    WorldType(
        name="barren",
        # Zero habitability: nothing here sustains life on its own, so a colony
        # survives only on artificial life support and whatever a supply line
        # brings it. Note the metal yields -- these are among the richest worlds
        # in the table, which is the trade: the best mining is on the worlds
        # least able to keep anyone alive.
        habitability=(0.0, 0.0),
        hazard=(0.2, 0.5),
        yields={METAL: (0.8, 1.5), ENERGY: (0.1, 0.4)},
        slots=(2, 5),
        weight=1.4,
    ),
    WorldType(
        name="gas_giant",
        habitability=(0.0, 0.0),
        hazard=(0.4, 0.8),
        yields={ENERGY: (1.2, 2.2), VOLATILES: (1.0, 2.0)},
        slots=(2, 4),
        weight=1.1,
    ),
)

WORLD_TYPES_BY_NAME: dict[str, WorldType] = {t.name: t for t in WORLD_TYPES}

#: Types a starting homeworld may be. A civ must open the game able to grow, so
#: the fair-start guarantee is a type floor rather than a reroll on a bad draw.
HABITABLE_TYPES: tuple[str, ...] = ("terrestrial", "ocean")


def weighted_world_type(rng: random.Random) -> WorldType:
    """Pick a world type by weight."""
    total = sum(t.weight for t in WORLD_TYPES)
    roll = rng.uniform(0.0, total)
    upto = 0.0
    for world_type in WORLD_TYPES:
        upto += world_type.weight
        if roll <= upto:
            return world_type
    return WORLD_TYPES[-1]  # unreachable outside float rounding


def roll_world(rng: random.Random, type_name: str | None = None) -> RolledWorld:
    """Roll a world, of ``type_name`` if given or a weighted pick if not."""
    world_type = WORLD_TYPES_BY_NAME[type_name] if type_name else weighted_world_type(rng)
    return world_type.roll(rng)
