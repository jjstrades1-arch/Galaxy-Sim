"""Tick resolvers, run in the order defined by :mod:`galaxysim.engine.tick`."""

from galaxysim.engine.resolvers import (
    colonization,
    combat,
    governor,
    logistics,
    movement,
    production,
    research,
    terraform,
)

__all__ = [
    "movement",
    "logistics",
    "combat",
    "governor",
    "production",
    "research",
    "colonization",
    "terraform",
]
