"""World and system generation.

Currently holds the world type table and the roll that turns a type into
concrete stats. Build-order step 4 adds ``system_at(universe_seed, sector)`` --
the pure function that makes space infinite and lazily materialized.
"""

from galaxysim.worldgen.types import WORLD_TYPES, WorldType, roll_world, weighted_world_type

__all__ = ["WORLD_TYPES", "WorldType", "roll_world", "weighted_world_type"]
