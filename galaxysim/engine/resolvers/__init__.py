"""Tick resolvers, run in the order defined by :mod:`galaxysim.engine.tick`."""

from galaxysim.engine.resolvers import (
    colonization,
    combat,
    logistics,
    movement,
    production,
    research,
)

__all__ = ["movement", "logistics", "combat", "production", "research", "colonization"]
