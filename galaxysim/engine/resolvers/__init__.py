"""Tick resolvers, run in the order defined by :mod:`galaxysim.engine.tick`."""

from galaxysim.engine.resolvers import colonization, combat, movement, production, research

__all__ = ["movement", "combat", "production", "research", "colonization"]
